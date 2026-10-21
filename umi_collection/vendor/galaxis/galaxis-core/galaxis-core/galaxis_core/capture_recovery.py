"""Durable UMI capture intent and recovery of complete MCAP records.

Only explicit discard is destructive. Recovery publishes a valid raw MCAP,
marked interrupted, without pretending that missing frames passed MP4 quality
checks. Originals remain available for diagnosis.
"""
import copy
import fcntl
from io import BytesIO
import json
import os
from pathlib import Path
import struct
import tempfile
import time
import uuid

from .recording_readiness import MAGIC, MAX_RECORD_BYTES

TERMINAL = {'saved', 'discarded', 'recovered', 'empty'}


def atomic_json(path, value):
    from .mp4_worker import atomic_json as write
    write(path, value)


def update_journal(path, **changes):
    path = Path(path)
    with path.with_suffix('.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = json.loads(path.read_text())
        if value['state'] in TERMINAL:
            return value
        # Snapshot callbacks must never erase the first interruption boundary.
        if 'interruption' in value:
            changes.pop('interruption', None)
        value.update(changes)
        atomic_json(path, value)
        return value


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def begin_capture(root, folder, metadata, required_topics, lease_file=''):
    root, folder = Path(root).resolve(), Path(folder).resolve()
    relative = folder.relative_to(root)
    directory = root/'.capture-journals'
    directory.mkdir(parents=True, exist_ok=True)
    sync_directory(root)
    path = directory/(uuid.uuid4().hex+'.json')
    atomic_json(path, dict(version=1, state='recording', folder=str(relative),
                          metadata=metadata, required_topics=sorted(required_topics),
                          started_ns=time.time_ns(), lease_file=str(lease_file)))
    return path


def interrupt_capture(path, reason, detected_ns=None):
    return update_journal(path, state='interrupted', interruption=dict(
        reason=reason, detected_ns=detected_ns or time.time_ns()))


def episode_folder(path, value=None):
    path = Path(path)
    value = value or json.loads(path.read_text())
    root = path.parent.parent.resolve()
    folder = (root/value['folder']).resolve()
    folder.relative_to(root)
    if folder == root:
        raise ValueError('Invalid recovery episode path')
    return folder


def pending_recoveries(root):
    for path in sorted((Path(root)/'.capture-journals').glob('*.json')):
        try:
            value = json.loads(path.read_text())
            if value['state'] in TERMINAL:
                continue
            folder = episode_folder(path, value)
        except (OSError, ValueError, KeyError):
            continue
        if not folder.exists():
            update_journal(path, state='empty', error='Recorder created no data directory')
            continue
        if (folder/'.discarded').exists():
            update_journal(path, state='discarded')
            continue
        if (folder/'.mp4-pending.json').exists() and not value.get('interruption'):
            # Normal stop durably queued conversion before the parent exited.
            update_journal(path, state='saved')
            continue
        failure = folder/'recording-failure.json'
        try:
            details = json.loads(failure.read_text())
            if details.get('stage') != 'recovery' or time.time() < details.get('retry_after', 0):
                continue
        except FileNotFoundError:
            pass
        except (ValueError, OSError):
            continue
        yield path, folder


def complete_messages(path, issues):
    """Read complete CRC-valid chunks even if a crash omitted the footer."""
    from mcap.data_stream import ReadDataStream
    from mcap.records import Schema, Channel, Chunk, Message
    from mcap.stream_reader import breakup_chunk
    schemas, channels = {}, {}
    with Path(path).open('rb') as source:
        if source.read(8) != MAGIC:
            issues.append(f'{Path(path).name}: invalid header')
            return
        while True:
            header = source.read(9)
            if not header or header == MAGIC:
                break
            if len(header) < 9:
                issues.append(f'{Path(path).name}: incomplete tail')
                break
            opcode, length = struct.unpack('<BQ', header)
            if length > MAX_RECORD_BYTES:
                issues.append(f'{Path(path).name}: invalid record length')
                break
            payload = source.read(length)
            if len(payload) != length:
                issues.append(f'{Path(path).name}: incomplete record')
                break
            if opcode not in (3, 4, 5, 6):
                continue
            try:
                stream = ReadDataStream(BytesIO(payload))
                if opcode == 3: records = [Schema.read(stream)]
                elif opcode == 4: records = [Channel.read(stream)]
                elif opcode == 5: records = [Message.read(stream, length)]
                else: records = list(breakup_chunk(Chunk.read(stream), validate_crc=True))
                for record in records:
                    if isinstance(record, Schema): schemas[record.id] = record
                    elif isinstance(record, Channel): channels[record.id] = record
                    elif isinstance(record, Message):
                        channel = channels[record.channel_id]
                        schema = schemas[channel.schema_id] if channel.schema_id else None
                        yield schema, channel, record
            except Exception as exc:
                # A damaged chunk cannot be declared valid. Preserve the good
                # prefix and original file; do not skip over a hidden data gap.
                issues.append(f'{Path(path).name}: {type(exc).__name__}')
                break


def inspect_recorded_continuity(sources):
    """Audit source Header timestamps before resampling or deleting raw data.

    100 ms is the current platform UMI screening limit, not a guarantee of
    training quality. Arrival delays alone must not change this decision.
    """
    first, last, issues = {}, {}, []
    previous_signals, signal_gaps = {}, []
    for source in sources:
        for _, channel, message in complete_messages(source, issues):
            first[channel.topic] = min(first.get(channel.topic, message.log_time), message.log_time)
            last[channel.topic] = max(last.get(channel.topic, message.log_time), message.log_time)
            # Match the platform's UMI check_series(..., gap=.1). Use recorded
            # source stamps, not callback arrival time. Never bridge a real
            # missing action interval when publishing the recovered prefix.
            if (channel.topic == '/joint_states' or
                    channel.topic.startswith('/pico/') and channel.topic.endswith('/state') or
                    channel.topic.endswith(('/image_raw/compressed', '/image_raw')) or
                    channel.topic.startswith('/galaxis/gripper/') and channel.topic.endswith('/joint_state')):
                data = message.data
                if channel.message_encoding == 'cdr' and len(data) >= 12 and data[:2] in (b'\x00\x01', b'\x00\x00'):
                    sec, nano = struct.unpack_from(('<' if data[1] else '>')+'iI', data, 4)
                    stamp = sec*1_000_000_000+nano
                    previous = previous_signals.get(channel.topic)
                    if previous and (stamp <= previous[0] or stamp-previous[0] > 100_000_000):
                        signal_gaps.append(dict(topic=channel.topic, gap_ns=stamp-previous[0],
                                                valid_until_log_ns=previous[1], resume_log_ns=message.log_time + (stamp <= previous[0])))
                    if channel.topic.startswith('/pico/controller/'):
                        # ROS2 CDR: Header, device/PC clocks, sequence, device id,
                        # xyz+xyzw, tracked, valid (same contract as UMI screening).
                        endian = '<' if data[1] else '>'
                        try:
                            pos = 16 + struct.unpack_from(endian+'I', data, 12)[0]
                            pos += (-(pos-4)) % 8
                            pos += 20
                            pos += (-(pos-4)) % 4
                            pos += 4 + struct.unpack_from(endian+'I', data, pos)[0]
                            pos += (-(pos-4)) % 8
                            pos += 56
                            if not data[pos] or not data[pos+1]:
                                signal_gaps.append(dict(topic=channel.topic, invalid_tracking=True,
                                                        valid_until_log_ns=previous[1] if previous else message.log_time-1,
                                                        resume_log_ns=message.log_time+1))
                        except (IndexError, struct.error):
                            issues.append(f'{channel.topic}: malformed controller payload')
                            signal_gaps.append(dict(topic=channel.topic, malformed=True,
                                                    valid_until_log_ns=previous[1] if previous else message.log_time-1,
                                                        resume_log_ns=message.log_time+1))
                    previous_signals[channel.topic] = (stamp, message.log_time)
    return first, last, issues, signal_gaps


def validate_recorded_continuity(folder):
    sources = sorted(Path(folder).glob('*.mcap'))
    _, _, issues, gaps = inspect_recorded_continuity(sources)
    if issues or gaps:
        detail = issues[0] if issues else gaps[0]['topic']
        raise ValueError(f'采集源数据连续性不合格: {detail}; 保留原始数据并恢复有效前缀')


def recover_episode(journal):
    import yaml
    from mcap.writer import Writer
    from .ownership import normalize_tree_access, resolve_record_owner
    journal = Path(journal)
    value = json.loads(journal.read_text())
    if value['state'] in TERMINAL:
        return
    folder = episode_folder(journal, value)
    if not folder.is_dir():
        raise ValueError('Recorder created no data directory')
    # The finalizer owns the capture/video locks before invoking recovery.
    # This marker also prevents an uploader from racing a retried publication.
    (folder/'.recovery-pending').touch()
    sources = sorted(p for p in folder.glob('*.mcap') if p.name != 'recovered.mcap')
    first, last, issues, signal_gaps = inspect_recorded_continuity(sources)
    if not last:
        raise ValueError('No complete recorded messages could be recovered; originals retained')
    from .recording_readiness import required_recording_topics
    required = set(value['required_topics']) or required_recording_topics(first)
    value['required_topics'] = sorted(required)
    observed = required & last.keys()
    missing = sorted(required-last.keys())
    # Stop at the last sample common to the required live streams, rather
    # than counting detection delay or the next restart as recorded duration.
    end_ns = min(last[t] for t in observed) if observed else max(last.values())
    start_ns = max(first[t] for t in observed) if observed else min(first.values())
    old_episode = value['metadata'].get('episode') or {}
    requested_start = old_episode.get('start_time')
    if isinstance(requested_start, (int, float)) and start_ns <= requested_start*1e9 < end_ns:
        start_ns = int(requested_start*1e9)
    interruption = value.get('interruption', dict(reason='recorder_or_app_exited'))
    if start_ns < interruption.get('detected_ns', end_ns) < end_ns:
        end_ns = interruption['detected_ns']
    if end_ns < start_ns:
        # No common interval: keep captured evidence, labelled incomplete.
        start_ns, end_ns = min(first.values()), max(last.values())
        missing.append('no_common_stream_interval')
    intervals = continuous_intervals(start_ns, end_ns, signal_gaps)
    if not intervals:
        # Keep evidence even if nothing passes continuity. Never label it as a
        # usable training episode; upload/review remains possible.
        intervals = [(start_ns, end_ns)]
        missing.append('no_continuous_valid_interval')
    outputs = []
    for index, (start, end) in enumerate(intervals):
        target = folder if index == 0 else folder.with_name(folder.name + f'__part{index+1:03d}')
        part = dict(part_index=index+1, part_count=len(intervals),
                    original_episode_folder=folder.name)
        if target != folder and target.exists():
            existing = target/'capture-recovery.json'
            if not existing.exists() and any(target.iterdir()):
                # A directory left by our own interrupted publication may only
                # contain our marker or temporary repair output.
                if not (target/'.recovery-pending').exists():
                    raise ValueError(f'Recovery part path already exists: {target.name}')
            elif existing.exists():
                report = json.loads(existing.read_text())
                if any(report.get(k) != v for k,v in part.items()):
                    raise ValueError('Recovery part belongs to another episode')
        target.mkdir(exist_ok=True)
        if not (target/'.data-platform-uploaded.json').exists():
            (target/'.recovery-pending').touch()
            publish_recovered_interval(target, sources, value, interruption, missing,
                                       issues, signal_gaps, start, end, part)
        outputs.append(target)
    # Publish the complete split set before releasing any upload barriers.
    for target in outputs:
        (target/'.mp4-pending.json').unlink(missing_ok=True)
        (target/'.recovery-pending').unlink(missing_ok=True)
        sync_directory(target)
    update_journal(journal, state='recovered', recovered_parts=[p.name for p in outputs])


def continuous_intervals(start_ns, end_ns, gaps):
    """Remove missing/invalid spans without joining actions across a gap."""
    intervals = [(start_ns, end_ns)]
    for gap in sorted(gaps, key=lambda g: g['valid_until_log_ns']):
        before, after = gap['valid_until_log_ns'], gap['resume_log_ns']
        remaining = []
        for start, end in intervals:
            if after <= start or before >= end:
                remaining.append((start, end))
                continue
            if start < before:
                remaining.append((start, min(end, before)))
            if after < end:
                remaining.append((max(start, after), end))
        intervals = remaining
    return [(a,b) for a,b in intervals if b > a]


def publish_recovered_interval(folder, sources, value, interruption, missing,
                               issues, signal_gaps, start_ns, end_ns, part):
    import yaml
    from mcap.writer import Writer
    from .ownership import normalize_tree_access, resolve_record_owner
    old_episode = value['metadata'].get('episode') or {}
    counts, channels_info = {}, {}
    bag_first, bag_last = end_ns, start_ns
    with tempfile.TemporaryDirectory(prefix='.capture-repair-', dir=folder) as temporary:
        output = Path(temporary)/'recovered.mcap'
        with output.open('wb') as target:
            writer = Writer(target, enable_crcs=True); writer.start(profile='ros2')
            for source in sources:
                schema_ids, channel_ids = {}, {}
                for schema, channel, message in complete_messages(source, []):
                    latched = channel.topic in ('/tf_static', '/device_ids', '/collection_profile') or channel.topic.endswith(('/encoder_offset', '/id'))
                    if message.log_time > end_ns or message.log_time < start_ns and not latched:
                        continue
                    if channel.id not in channel_ids:
                        if schema and schema.id not in schema_ids:
                            schema_ids[schema.id] = writer.register_schema(schema.name, schema.encoding, schema.data)
                        channel_ids[channel.id] = writer.register_channel(channel.topic,
                            channel.message_encoding, schema_ids[schema.id] if schema else 0, channel.metadata)
                    writer.add_message(channel_ids[channel.id], message.log_time, message.data,
                                       message.publish_time, message.sequence)
                    bag_first, bag_last = min(bag_first, message.log_time), max(bag_last, message.log_time)
                    counts[channel.topic] = counts.get(channel.topic, 0)+1
                    channels_info[channel.topic] = dict(name=channel.topic,
                        type=schema.name if schema else '', serialization_format=channel.message_encoding,
                        offered_qos_profiles=[], type_description_hash=channel.metadata.get('type_description_hash', ''))
            writer.finish(); target.flush(); os.fsync(target.fileno())
        if not counts: raise ValueError('No complete messages within recovered interval')
        os.replace(output, folder/'recovered.mcap')
    report = dict(interruption, recovered=True, start_ns=start_ns, end_ns=end_ns,
                  end_time_basis='last_common_recorded_sample',
                  missing_topics=sorted(set(missing) | (set(value['required_topics'])-counts.keys())),
                  source_issues=issues, original_files=[p.name for p in sources],
                  signal_timestamp_gaps=signal_gaps, **part,
                  quality_validation='interrupted_raw_not_certified_complete')
    info = dict(version=9, storage_identifier='mcap',
        duration=dict(nanoseconds=bag_last-bag_first), starting_time=dict(nanoseconds_since_epoch=bag_first),
        message_count=sum(counts.values()), compression_format='', compression_mode='',
        custom_data={}, ros_distro=os.environ.get('ROS_DISTRO', 'jazzy'),
        relative_file_paths=['recovered.mcap'],
        topics_with_message_count=[dict(topic_metadata=channels_info[t], message_count=n) for t,n in counts.items()],
        files=[dict(path='recovered.mcap', starting_time=dict(nanoseconds_since_epoch=bag_first),
                    duration=dict(nanoseconds=bag_last-bag_first), message_count=sum(counts.values()))])
    metadata = copy.deepcopy(value['metadata'])
    if part['part_count'] > 1:
        metadata['uuid'] = str(uuid.uuid5(uuid.NAMESPACE_URL, str(value['metadata'].get('uuid', '')) + ':' + str(folder)))
    metadata['files'] = [dict(type='mcap', name='recovered.mcap')]
    metadata['recording_format'] = 'rosbag2-mcap'
    metadata['interruption'] = report
    metadata['episode'] = dict(old_episode, start_time=start_ns/1e9, end_time=end_ns/1e9, success=False)
    segments = []
    for segment in metadata.get('segments', []):
        segment = dict(segment)
        a, b = segment.get('start_time'), segment.get('end_time')
        if isinstance(a, (int,float)) and isinstance(b, (int,float)):
            a, b = max(a, start_ns/1e9), min(b, end_ns/1e9)
            if b <= a: continue
            if a != segment['start_time'] or b != segment['end_time']:
                segment['success'] = False
            segment.update(start_time=a, end_time=b)
        segments.append(segment)
    metadata['segments'] = segments
    # Metadata and the valid replacement are published before clearing the
    # upload barrier. A crash at any step is safe to retry from the originals.
    temp_yaml = folder/'.recovery-metadata.yaml'
    with temp_yaml.open('w') as output:
        yaml.safe_dump({'rosbag2_bagfile_information':info}, output, sort_keys=False)
        output.flush(); os.fsync(output.fileno())
    os.replace(temp_yaml, folder/'metadata.yaml')
    atomic_json(folder/'capture-recovery.json', report)
    atomic_json(folder/'meta.json', metadata)
    (folder/'recording-failure.json').unlink(missing_ok=True)
    normalize_tree_access(str(folder), *resolve_record_owner(str(folder)))
    sync_directory(folder)

