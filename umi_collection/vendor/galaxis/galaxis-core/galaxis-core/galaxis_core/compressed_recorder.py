"""Record compressed, self-contained ROS 2 MCAP directly, with bounded queues.

No full-size JPEG bag is written. .partial denotes an unfinished recording;
the .mcap name is published only after the encoder drains and the footer and
metadata are durable. The normal capture journal recovers interrupted files.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import sys
import threading
import time

from mcap.writer import Writer, CompressionType

from .live_encoder import LiveEncoder
from .mcap_video import FORMAT, VIDEO_TYPE, VIDEO_SCHEMA, video_message
from .mp4_worker import atomic_json
from .video_bundle import CAMERAS, split_image
from .reception_metrics import ReceptionMetrics, message_info_value
from .recording_queue import RecordingQueue


class DurableStream:
    def __init__(self, path):
        self.stream = path.open('xb', buffering=0)
        self.digest = hashlib.sha256()

    def write(self, data):
        view = memoryview(data)
        while view:
            count = self.stream.write(view)
            if not count:
                raise OSError('Storage write made no progress')
            self.digest.update(view[:count])
            view = view[count:]
        return len(data)

    def tell(self):
        return self.stream.tell()


class CompressedBag:
    def __init__(self, folder, *, native=None, defer_video=False, encoder_timeout=0):
        self.defer_video = defer_video
        self.encoder_timeout = encoder_timeout
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=False)
        self.path = self.folder / (self.folder.name+'_0.mcap')
        self.partial = self.path.with_suffix('.mcap.partial')
        self.stream = DurableStream(self.partial)
        self.writer = Writer(self.stream, chunk_size=256*1024, compression=CompressionType.ZSTD,
                             enable_crcs=True, enable_data_crcs=True)
        self.writer.start(profile='ros2', library='galaxis-compressed-recorder')
        self.lock = threading.Lock()
        self.channels = {}
        self.counts = {}
        self.encoders = {}
        self.native = native
        self.first = None
        self.last = None
        self.device = self.folder.stat().st_dev
        self.closed = False
        self.sizes = {}
        self.previous_samples = {}
        self.continuity_issue = None
        self.continuity_summary = {}
        self.reception = ReceptionMetrics()

    def topic(self, topic, type_name, definition, qos=''):
        if topic in self.channels:
            return
        original_type = type_name
        if topic in CAMERAS and not self.defer_video:
            if type_name != 'sensor_msgs/msg/CompressedImage':
                raise ValueError('Only JPEG CompressedImage inputs are supported')
            type_name, definition = VIDEO_TYPE, VIDEO_SCHEMA
        with self.lock:
            sid = self.writer.register_schema(type_name, 'ros2msg', definition)
            cid = self.writer.register_channel(topic, 'cdr', sid, {'offered_qos_profiles': qos,
                'recording_format': FORMAT, 'source_type': original_type})
            self.channels[topic] = dict(id=cid, type=type_name, qos=qos)
            self.counts[topic] = 0

    def message(self, topic, payload, log_time, publish_time=None, sequence=0):
        stamp = (int(log_time), int(publish_time or log_time), int(sequence) & 0xffffffff)
        if topic in CAMERAS and not self.defer_video:
            prefix, jpeg = split_image(payload)
            import io
            from PIL import Image
            with Image.open(io.BytesIO(jpeg)) as image:
                size = image.size
            if topic in self.sizes and self.sizes[topic] != size:
                raise ValueError('录制过程中相机分辨率发生变化，保留当前回合')
            self.sizes[topic] = size
            if topic not in self.encoders:
                def output(packet, metadata):
                    header, timing = metadata
                    self._write(topic, video_message(header, packet), timing)
                self.encoders[topic] = LiveEncoder(output, native=self.native, managed=self.encoder_timeout is None)
            self.encoders[topic].submit(jpeg, (prefix, stamp), timeout=self.encoder_timeout)
        else:
            self._write(topic, payload, stamp)

    def _write(self, topic, payload, stamp):
        log_time, publish_time, sequence = stamp
        with self.lock:
            # Audit while streaming, so normal saves never rescan hours of
            # video. A gap is retained for recovery after stop, not hidden by
            # interpolation and not an extra immediate stop condition.
            from .capture_recovery import check_recorded_sample, requires_interval_recovery
            gaps, issues = [], []
            check_recorded_sample(topic, 'cdr', payload, log_time,
                                  self.previous_samples, gaps, issues)
            for gap in gaps:
                row = self.continuity_summary.setdefault(topic, dict(
                    gaps_over_100ms=0, warnings_over_200ms=0, max_gap_ns=0, examples=[]))
                row['gaps_over_100ms'] += 1
                row['warnings_over_200ms'] += int(gap.get('gap_ns', 0) > 200_000_000)
                row['max_gap_ns'] = max(row['max_gap_ns'], gap.get('gap_ns', 0))
                if len(row['examples']) < 16:
                    row['examples'].append(gap)
            hard = [gap for gap in gaps if requires_interval_recovery(gap)]
            if self.continuity_issue is None and (hard or issues):
                self.continuity_issue = issues[0] if issues else hard[0]
            self.writer.add_message(self.channels[topic]['id'], log_time, payload,
                                    publish_time, sequence)
            self.counts[topic] += 1
            self.first = log_time if self.first is None else min(self.first, log_time)
            self.last = log_time if self.last is None else max(self.last, log_time)

    def check(self):
        for encoder in self.encoders.values():
            encoder.check()
        if self.folder.stat().st_dev != self.device:
            raise OSError('采集磁盘已掉线，不会切回系统盘继续写入')
        disk = os.statvfs(self.folder)
        if disk.f_bavail*disk.f_frsize < 512*1024*1024:
            raise OSError('采集盘剩余空间不足，结束并保存当前回合')
        # fsync must not hold the writer lock: slow media must not stop ROS
        # callbacks from draining DDS. The checker is joined before close().
        os.fsync(self.stream.stream.fileno())

    def close(self, *, complete=True):
        if self.closed:
            return
        self.closed = True
        errors = []
        for encoder in self.encoders.values():
            try:
                encoder.close()
            except Exception as exc:
                errors.append(str(exc))
        try:
            if self.reception.topics:
                try:
                    atomic_json(self.folder/'capture-transport.json', self.reception.snapshot())
                except OSError as exc:
                    # Diagnostics must never prevent the original bag footer
                    # from being flushed or turn a valid round into a failure.
                    print(f'接收统计保存失败，继续保存原始数据: {exc}', file=sys.stderr)
            with self.lock:
                self.writer.finish()
                os.fsync(self.stream.stream.fileno())
            if errors or not complete:
                raise RuntimeError('; '.join(errors) or 'Recording interrupted')
            if self.first is None:
                raise RuntimeError('No messages recorded')
            import yaml
            info = dict(version=9, storage_identifier='mcap',
                relative_file_paths=[self.path.name],
                duration=dict(nanoseconds=self.last-self.first),
                starting_time=dict(nanoseconds_since_epoch=self.first),
                message_count=sum(self.counts.values()), compression_format='', compression_mode='',
                custom_data={'recording_format': FORMAT}, ros_distro=os.getenv('ROS_DISTRO', 'jazzy'),
                topics_with_message_count=[dict(topic_metadata=dict(name=t, type=c['type'],
                    serialization_format='cdr', offered_qos_profiles=[], type_description_hash=''),
                    message_count=self.counts[t]) for t,c in self.channels.items()],
                files=[dict(path=self.path.name, starting_time=dict(nanoseconds_since_epoch=self.first),
                            duration=dict(nanoseconds=self.last-self.first), message_count=sum(self.counts.values()))])
            if self.defer_video:
                # Only the offline finalizer may publish a ready .mcap. The
                # JPEG staging stream is durable and recoverable after a crash.
                self.stream.stream.close()
                atomic_json(self.folder/'.deferred-raw.json', dict(
                    format='umi-jpeg-staging-v1', state='captured', file=self.partial.name,
                    counts=self.counts, continuity_issue=self.continuity_issue,
                    continuity_summary=self.continuity_summary))
                atomic_json(self.folder/'raw-data-status.json', dict(
                    state='pending', message='采集已保存，等待压缩为正式原始数据'))
                return
            tmp = self.folder/'metadata.yaml.tmp'
            with tmp.open('w') as handle:
                yaml.safe_dump({'rosbag2_bagfile_information': info}, handle, sort_keys=False)
                handle.flush(); os.fsync(handle.fileno())
            self.stream.stream.close()
            os.replace(self.partial, self.path)
            os.replace(tmp, self.folder/'metadata.yaml')
            atomic_json(self.folder/'.compressed-raw.json', dict(format=FORMAT, state='complete',
                file=self.path.name, sha256=self.stream.digest.hexdigest(),
                message_count=sum(self.counts.values()), camera_frames={t:e.count for t,e in self.encoders.items()},
                camera_sizes=self.sizes,
                continuity_issue=self.continuity_issue, continuity_summary=self.continuity_summary,
                encoder='rockchip-h264-qp20' if self.native else 'libx264-crf18-zerolatency'))
        finally:
            self.stream.stream.close()


def message_definition(type_name):
    """Embed the installed .msg and all nested types, including custom sensors."""
    from rosidl_runtime_py import get_interface_path
    seen = {}
    def visit(name):
        if name in seen:
            return
        package, _, short = name.split('/')
        text = Path(get_interface_path(name+'.msg')).read_text()
        seen[name] = text
        for line in text.splitlines():
            line = line.split('#', 1)[0].strip()
            if not line or '=' in line.split(maxsplit=1)[0]:
                continue
            field = re.split(r'\[|<=', line.split()[0])[0]
            if field in ('bool','byte','char','float32','float64','string','wstring') or re.fullmatch(r'u?int(?:8|16|32|64)', field):
                continue
            if field == 'Header':
                nested = 'std_msgs/msg/Header'
            elif '/' not in field:
                nested = f'{package}/msg/{field}'
            else:
                bits = field.split('/')
                nested = f'{bits[0]}/msg/{bits[-1]}'
            visit(nested)
    visit(type_name)
    definition = seen[type_name]
    for name, text in seen.items():
        if name != type_name:
            definition += '\n'+'='*80+'\nMSG: '+name.replace('/msg/', '/')+'\n'+text
    return definition.encode()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-o', '--output', required=True, type=Path)
    parser.add_argument('--native', type=Path)
    parser.add_argument('--defer-video', action='store_true')
    parser.add_argument('--qos-profile-overrides-path')
    parser.add_argument('topics', nargs='+')
    args = parser.parse_args()
    native = args.native
    if not args.defer_video and platform.machine() in ('aarch64', 'arm64') and native is None:
        native = Path(__file__).parent/'native/mpp-jpeg-h264'
        if not native.is_file():
            raise RuntimeError('缺少板载视频编码组件，未开始录制；请更新完整运行环境')
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy, LivelinessPolicy
    from rclpy.duration import Duration
    from rosidl_runtime_py.utilities import get_message
    rclpy.init()
    node = Node('compressed_mcap_recorder')
    overrides = {}
    if args.qos_profile_overrides_path:
        import yaml
        overrides = yaml.safe_load(Path(args.qos_profile_overrides_path).read_text()) or {}
    bag = CompressedBag(args.output, native=native, defer_video=args.defer_video)
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    subscriptions = {}
    failure = []
    def write_received(item):
        topic, payload, arrival, source, sequence = item
        started_ns = time.monotonic_ns()
        bag.message(topic, payload, arrival, source, sequence)
        bag.reception.written(topic, time.monotonic_ns() - started_ns)
    inbox = RecordingQueue(write_received)
    def discover():
        available = dict(node.get_topic_names_and_types())
        for topic in args.topics:
            if topic in subscriptions or topic not in available:
                continue
            # Graph types include subscribers, which cannot define recorded data.
            offers = node.get_publishers_info_by_topic(topic)
            types = {offer.topic_type for offer in offers}
            if not types:
                continue
            if len(types) != 1:
                raise ValueError('Conflicting publisher message types on '+topic)
            type_name = types.pop()
            # Match a publisher's durability for latched calibration/static TF.
            latched = offers and all(o.qos_profile.durability == DurabilityPolicy.TRANSIENT_LOCAL for o in offers)
            reliable = offers and all(o.qos_profile.reliability == ReliabilityPolicy.RELIABLE for o in offers)
            qos = QoSProfile(depth=60, reliability=ReliabilityPolicy.RELIABLE if reliable else ReliabilityPolicy.BEST_EFFORT,
                durability=DurabilityPolicy.TRANSIENT_LOCAL if latched else DurabilityPolicy.VOLATILE)
            for key, value in overrides.get(topic, {}).items():
                enums = {'reliability': ReliabilityPolicy, 'durability': DurabilityPolicy,
                         'history': HistoryPolicy, 'liveliness': LivelinessPolicy}
                if key in enums:
                    value = enums[key].get_from_short_key(value)
                elif key in ('deadline', 'lifespan', 'liveliness_lease_duration'):
                    value = Duration(seconds=value.get('sec', 0), nanoseconds=value.get('nsec', 0))
                elif key not in ('depth', 'avoid_ros_namespace_conventions'):
                    raise ValueError('Unsupported recording QoS setting: '+key)
                setattr(qos, key, value)
            bag.topic(topic, type_name, message_definition(type_name))
            def callback(data, info, topic=topic):
                try:
                    arrival = message_info_value(info, 'received_timestamp') or node.get_clock().now().nanoseconds
                    source = message_info_value(info, 'source_timestamp') or arrival
                    bag.reception.received(topic, data, arrival, source, time.time_ns())
                    inbox.submit((topic, data, arrival, source,
                                  message_info_value(info, 'publication_sequence_number')), len(data))
                except Exception as exc:
                    reason = f'{topic}: {type(exc).__name__}: {exc}'
                    failure.append(reason)
                    print('录制接收失败：' + reason, file=sys.stderr, flush=True)
                    stopped.set()
            subscriptions[topic] = node.create_subscription(get_message(type_name), topic,
                                                             callback, qos, raw=True)
    def check_storage():
        while not stopped.wait(.5):
            try:
                bag.check()
            except Exception as exc:
                failure.append(f'存储检查失败: {exc}')
                stopped.set()
    checker = threading.Thread(target=check_storage, name='recording-storage-check')
    checker.start()
    last_check = 0
    try:
        while rclpy.ok() and not stopped.is_set():
            inbox.check()
            now = time.monotonic()
            if now-last_check > .5:
                discover(); last_check = now
            rclpy.spin_once(node, timeout_sec=.02)
        stopped.set()
        checker.join()
        inbox.close()
        bag.reception.writer_queue = inbox.statistics()
        if failure:
            raise RuntimeError(failure[0])
        bag.close()
    except Exception as exc:
        stopped.set()
        checker.join()
        try:
            inbox.close()
        except Exception:
            pass  # Preserve the original error and finalize the recoverable bag.
        bag.reception.writer_queue = inbox.statistics()
        # Finalization of a failed round deliberately raises "interrupted".
        # It must never replace the original receiving/encoding exception.
        try:
            atomic_json(bag.folder / '.recorder-error.json', {
                'reason': str(exc), 'type': type(exc).__name__, 'detected_ns': time.time_ns(),
                'encoders': {topic: {'accepted': e.accepted, 'written': e.count,
                    'queued': e.input.qsize(), 'pending': e.pending.qsize(),
                    'failure': e.failure, 'stderr': list(e.errors)} for topic, e in bag.encoders.items()}})
        except OSError:
            pass
        try:
            bag.close(complete=False)
        except Exception:
            pass
        raise
    finally:
        stopped.set()
        checker.join()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
