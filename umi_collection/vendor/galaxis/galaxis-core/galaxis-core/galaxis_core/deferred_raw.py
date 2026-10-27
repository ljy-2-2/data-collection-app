"""Publish a self-contained compressed raw bag after capture has released I/O.

The pending marker is the transaction barrier for upload. A durable JPEG
staging file is retained until every frame has been encoded and published.
"""
import json
import os
from pathlib import Path
import platform
import shutil

import yaml

from .compressed_recorder import CompressedBag
from .capture_recovery import complete_messages, requires_interval_recovery
from .mp4_worker import atomic_json


class RawCaptureIntegrityError(ValueError):
    """The captured source itself needs recovery, independently of encoding."""


def finalize_raw(folder, metadata, *, source=None, native=None):
    folder = Path(folder)
    marker = folder / '.compressed-raw.json'
    # A power cut after publication but before queue acknowledgement is safe.
    if marker.is_file():
        saved = json.loads(marker.read_text())
        if (saved.get('state') == 'complete' and saved.get('format') == 'umi-mcap-h264-v1'
                and Path(saved['file']).name == saved['file']
                and (folder / saved['file']).is_file()):
            _publish_metadata(folder, metadata, saved)
            _cleanup_staging(folder, saved)
            return
    normal_capture = source is None
    if source is None:
        staging = json.loads((folder / '.deferred-raw.json').read_text())
        name = staging['file']
        if Path(name).name != name or not name.endswith('.mcap.partial'):
            raise ValueError('Invalid raw staging filename')
        if requires_interval_recovery(staging.get('continuity_issue')):
            raise RawCaptureIntegrityError('暂存数据存在真实缺口，保留原始数据等待区间恢复')
        source = folder / name
    source = Path(source)
    if source.is_symlink():
        raise ValueError('Raw staging must be a regular file')
    if native is None and platform.machine() in ('aarch64', 'arm64'):
        native = Path(__file__).parent / 'native/mpp-jpeg-h264'
    atomic_json(folder / 'raw-data-status.json', dict(state='processing',
        message='正在压缩整理原始数据，暂存文件已保留'))
    stage = folder / '.raw-finalizing'
    if stage.exists():
        if stage.is_symlink() or not (stage / '.owned-raw-stage').is_file():
            raise ValueError('Unrecognized raw finalization directory; preserved')
        shutil.rmtree(stage)
    bag = CompressedBag(stage, native=native, encoder_timeout=None)
    (stage / '.owned-raw-stage').write_text('umi-mcap-h264-v1')
    from .finalizer_pause import checkpoint
    counts, issues = {}, []
    try:
        for schema, channel, message in complete_messages(source, issues):
            if not schema or channel.message_encoding != 'cdr':
                raise ValueError('Unsupported raw recording schema')
            checkpoint(bag.encoders)
            bag.topic(channel.topic, schema.name, schema.data,
                      channel.metadata.get('offered_qos_profiles', ''))
            bag.message(channel.topic, message.data, message.log_time,
                        message.publish_time, message.sequence)
            counts[channel.topic] = counts.get(channel.topic, 0) + 1
        if normal_capture and counts != {t: n for t, n in staging.get('counts', {}).items() if n}:
            raise RawCaptureIntegrityError('Staging message counts differ from capture; originals retained')
        if issues:
            raise RawCaptureIntegrityError('Damaged raw staging file: ' + '; '.join(issues))
        checkpoint(bag.encoders)
        bag.close()
        if normal_capture and bag.continuity_issue:
            raise RawCaptureIntegrityError('原始数据连续性检查失败，暂存文件保留')
        if bag.counts != counts:
            raise ValueError('Raw compression changed message counts; staging retained')
        saved = json.loads((stage / '.compressed-raw.json').read_text())
        final_name = folder.name + '_0.mcap'
        info = yaml.safe_load((stage / 'metadata.yaml').read_text())
        info['rosbag2_bagfile_information']['relative_file_paths'] = [final_name]
        info['rosbag2_bagfile_information']['files'][0]['path'] = final_name
        temporary = folder / '.raw-metadata.yaml'
        with temporary.open('w') as stream:
            yaml.safe_dump(info, stream, sort_keys=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(bag.path, folder / final_name)
        os.replace(temporary, folder / 'metadata.yaml')
        saved['file'] = final_name
        saved['rosbag_metadata'] = info
        saved['finalized_after_capture'] = True
        if normal_capture:
            saved['staging_cleanup'] = source.name
        atomic_json(marker, saved)
        _publish_metadata(folder, metadata, saved)
        _cleanup_staging(folder, saved)
        (folder / 'recording-failure.json').unlink(missing_ok=True)
        shutil.rmtree(stage)
    except BaseException:
        try:
            bag.close(complete=False)
        except Exception:
            pass
        raise


def _publish_metadata(folder, metadata, saved):
    meta = dict(metadata)
    meta.pop('_deferred_raw', None)
    meta.update(recording_format=saved['format'], raw_compression=saved['encoder'],
                raw_sha256=saved['sha256'], files=[dict(type='mcap', name=saved['file'])])
    meta['source_continuity'] = saved.get('continuity_summary', {})
    if saved.get('rosbag_metadata'):
        temporary = folder / '.raw-metadata.yaml'
        with temporary.open('w') as stream:
            yaml.safe_dump(saved['rosbag_metadata'], stream, sort_keys=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, folder / 'metadata.yaml')
    atomic_json(folder / 'meta.json', meta)
    atomic_json(folder / 'raw-data-status.json', dict(state='ready',
        message='原始数据已就绪', file=saved['file'], sha256=saved['sha256'],
        quality=('interrupted_requires_review' if meta.get('interruption') else
                 'source_gaps_require_review' if meta['source_continuity'] else 'continuity_checked'),
        source_continuity=meta['source_continuity']))


def _cleanup_staging(folder, saved):
    path = folder / '.deferred-raw.json'
    if not path.is_file():
        return
    value = json.loads(path.read_text())
    name = value.get('file', '')
    if (saved.get('staging_cleanup') == name and value.get('format') == 'umi-jpeg-staging-v1' and
            Path(name).name == name and name.endswith('.mcap.partial')):
        (folder / name).unlink(missing_ok=True)
        path.unlink()
