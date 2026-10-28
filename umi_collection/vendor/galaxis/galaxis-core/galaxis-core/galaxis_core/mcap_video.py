"""Self-contained ROS 2 MCAP video messages (Foxglove CompressedVideo).

Video uses Annex B H.264, one access unit per original camera timestamp,
without B frames. Sensor messages keep their original CDR bytes. This module
has no ROS dependency and is also shipped with the platform importer.
"""
import struct

FORMAT = 'umi-mcap-h264-v1'
VIDEO_TYPE = 'foxglove_msgs/msg/CompressedVideo'
VIDEO_SCHEMA = b'''builtin_interfaces/Time timestamp
string frame_id
uint8[] data
string format
================================================================================
MSG: builtin_interfaces/Time
int32 sec
uint32 nanosec
'''
IMAGE_TYPE = 'sensor_msgs/msg/CompressedImage'
IMAGE_SCHEMA = b'''std_msgs/Header header
string format
uint8[] data
================================================================================
MSG: std_msgs/Header
builtin_interfaces/Time stamp
string frame_id
================================================================================
MSG: builtin_interfaces/Time
int32 sec
uint32 nanosec
'''


def _string(data, offset, order):
    offset = 4 + ((offset - 4 + 3) // 4) * 4
    size = struct.unpack_from(order+'I', data, offset)[0]
    end = offset + 4 + size
    if size < 1 or end > len(data) or data[end-1] != 0:
        raise ValueError('Invalid CDR string')
    return data[offset+4:end-1], end


def _put_string(data, value):
    data.extend(b'\0' * ((-(len(data)-4)) % 4))
    data.extend(struct.pack('<I', len(value)+1) + value + b'\0')


def image_header(prefix):
    order = '<' if prefix[1] else '>'
    sec, nano = struct.unpack_from(order+'iI', prefix, 4)
    frame_id, _ = _string(prefix, 12, order)
    return sec, nano, frame_id


def video_message(prefix, packet):
    sec, nano, frame_id = image_header(prefix)
    data = bytearray(b'\0\1\0\0' + struct.pack('<iI', sec, nano))
    _put_string(data, frame_id)
    data.extend(b'\0' * ((-(len(data)-4)) % 4))
    data.extend(struct.pack('<I', len(packet)) + packet)
    _put_string(data, b'h264')
    return bytes(data)


def split_video(data):
    if data[:2] not in (b'\0\1', b'\0\0'):
        raise ValueError('Unsupported video CDR')
    order = '<' if data[1] else '>'
    sec, nano = struct.unpack_from(order+'iI', data, 4)
    frame_id, offset = _string(data, 12, order)
    offset = 4 + ((offset-4+3)//4)*4
    size = struct.unpack_from(order+'I', data, offset)[0]
    packet = data[offset+4:offset+4+size]
    codec, _ = _string(data, offset+4+size, order)
    if len(packet) != size or codec != b'h264':
        raise ValueError('Invalid H.264 message')
    prefix = bytearray(b'\0\1\0\0' + struct.pack('<iI', sec, nano))
    _put_string(prefix, frame_id)
    _put_string(prefix, b'jpeg')
    prefix.extend(b'\0' * ((-(len(prefix)-4)) % 4))
    return bytes(prefix), packet


def nal_types(packet):
    import re
    return [packet[m.end()] & 31 for m in re.finditer(b'\x00\x00(?:\x00)?\x01', packet)
            if m.end() < len(packet)]


def validate_packet(packet, *, first=False):
    types = nal_types(packet)
    if not ({1, 5} & set(types)) or (first and 5 not in types):
        raise ValueError('Missing complete video frame/keyframe')
    if 5 in types and not {7, 8} <= set(types):
        raise ValueError('Keyframe is missing SPS/PPS')
    return 5 in types


def is_compressed_bag(source):
    from mcap.reader import make_reader
    with source.open('rb') as stream:
        summary = make_reader(stream).get_summary()
        if summary is None:
            return False
        return any(s.name == VIDEO_TYPE for s in summary.schemas.values())


def extract_bundle(record_dir, metadata, source, *, report=lambda *a, **k: None):
    """Demux video packets for viewing/export; never re-encode/delete raw MCAP."""
    import json
    import os
    from pathlib import Path
    import subprocess
    import tempfile
    import yaml
    from collections import Counter
    from mcap.reader import NonSeekingReader
    from mcap.writer import Writer, CompressionType
    from . import video_bundle as bundle
    record_dir = Path(record_dir)
    with tempfile.TemporaryDirectory(prefix='.mp4-', dir=record_dir) as temp:
        stage = Path(temp)
        (stage/'.encoding-stage').write_text(bundle.FORMAT)
        cameras, streams, counts = {}, {}, Counter()
        schemas, channels = {}, {}
        signals = stage/'signals.mcap'
        try:
            with source.open('rb') as src, signals.open('wb') as dst:
                writer = Writer(dst, compression=CompressionType.ZSTD)
                writer.start(profile='ros2')
                source_size = source.stat().st_size
                for index, (schema, channel, message) in enumerate(NonSeekingReader(src, validate_crcs=True).iter_messages(log_time_order=False)):
                    if index % 1000 == 0:
                        report('scan_cameras', src.tell(), source_size, unit='bytes')
                    topic = channel.topic
                    if topic not in bundle.CAMERAS:
                        bundle._copy_message(writer, schemas, channels, schema, channel, message)
                        counts[topic] += 1
                        continue
                    if schema is None or schema.name != VIDEO_TYPE:
                        raise ValueError('Mixed JPEG/H.264 camera types in source bag')
                    prefix, packet = split_video(message.data)
                    validate_packet(packet, first=topic not in cameras)
                    if topic not in cameras:
                        side = bundle.CAMERAS[topic]
                        streams[topic] = (stage/(side+'.h264')).open('wb')
                        cameras[topic] = dict(topic=topic, file=side+'.mp4',
                            schema_name=IMAGE_TYPE, schema_encoding='ros2msg', schema_data=IMAGE_SCHEMA.hex(),
                            message_encoding='cdr', channel_metadata=channel.metadata, frames=[])
                    frames = cameras[topic]['frames']
                    if frames and message.log_time < frames[-1]['log_time']:
                        raise ValueError('Camera log timestamps moved backwards')
                    streams[topic].write(packet)
                    frames.append(dict(log_time=message.log_time, publish_time=message.publish_time,
                                       sequence=message.sequence, cdr_prefix=prefix.hex()))
                writer.finish()
            if '/center_camera/image_raw/compressed' not in cameras or len(cameras) < 2:
                raise ValueError('Compressed source requires center and a wrist camera')
            for stream in streams.values():
                stream.close()
            quality = {}
            contract_file = record_dir/'camera-contract.json'
            contract = json.loads(contract_file.read_text()) if contract_file.exists() else None
            for topic, camera in cameras.items():
                frames = camera['frames']
                stamps = [image_header(bytes.fromhex(f['cdr_prefix'])) for f in frames]
                times = [sec*10**9+nano for sec,nano,_ in stamps]
                if len(times)<2 or any(b<=a for a,b in zip(times,times[1:])):
                    raise ValueError('Compressed video timestamps are incomplete or unordered')
                fps = (len(times)-1)*1e9/(times[-1]-times[0])
                camera.update(fps=fps, frame_count=len(frames))
                if contract:
                    from .camera_quality import recording_timing_result
                    failure, warnings = recording_timing_result([f['log_time'] for f in frames],times,contract['limits'])
                    if failure:
                        raise ValueError('Compressed video timing: '+failure)
                    quality[bundle.CAMERAS[topic]] = dict(passed=True, warnings=warnings,
                        actual_fps=fps, frames=len(times),max_frame_gap_ms=max(b-a for a,b in zip(times,times[1:]))/1e6)
                report('encode_source_videos', 0, len(frames), unit='frames', item=bundle.CAMERAS[topic])
                raw = stage/(bundle.CAMERAS[topic]+'.h264')
                subprocess.run(['ffmpeg','-v','error','-nostdin','-y','-r',str(fps),'-f','h264','-i',str(raw),
                                '-c:v','copy','-movflags','+faststart',str(stage/camera['file'])],
                               check=True, capture_output=True)
                probe = bundle.probe_video(stage/camera['file'])
                if int(probe['nb_read_frames']) != len(frames):
                    raise ValueError('Compressed source/remux frame count mismatch')
                camera['size'] = [probe['width'],probe['height']]
                if contract:
                    wanted=contract['cameras'][bundle.CAMERAS[topic]]
                    if camera['size'] != [wanted['image_width'],wanted['image_height']]:
                        raise ValueError('Compressed video resolution differs from selected camera')
                camera['sha256'] = bundle.file_hash(stage/camera['file'])
                raw.unlink()
                report('encode_source_videos',len(frames),len(frames),unit='frames', item=bundle.CAMERAS[topic])
            if bundle._signal_digest(source) != bundle._signal_digest(signals):
                raise ValueError('Non-image source messages changed')
            manifest=dict(format=bundle.FORMAT,state='complete',source_format=FORMAT,
                encoder={'codec':'h264','operation':'copy'},source_file=source.name,
                source_sha256=bundle.file_hash(source),signals=dict(file=signals.name,sha256=bundle.file_hash(signals)),
                cameras=list(cameras.values()))
            if contract:
                manifest.update(camera_contract=contract,quality_validation=quality)
            (stage/bundle.MANIFEST).write_text(json.dumps(manifest,separators=(',',':')))
            info=metadata['rosbag2_bagfile_information']
            info['relative_file_paths']=[signals.name]; info['message_count']=sum(counts.values())
            info['topics_with_message_count']=[dict(row,message_count=counts[row['topic_metadata']['name']])
                for row in info.get('topics_with_message_count',[]) if row['topic_metadata']['name'] in counts]
            info['files']=[dict(path=signals.name,starting_time=info.get('starting_time'),duration=info.get('duration'),message_count=sum(counts.values()))]
            (stage/'metadata.yaml').write_text(yaml.safe_dump(metadata,sort_keys=False))
            bundle.validate_bundle(stage,decode=False)
            (stage/'.encoding-stage').unlink()
            for path in stage.iterdir():
                with path.open('rb') as f:
                    os.fsync(f.fileno())
            os.replace(stage,record_dir/'media')
            return manifest
        finally:
            for stream in streams.values():
                stream.close()
