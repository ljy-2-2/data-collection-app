"""Actual streaming encoders + self-contained raw MCAP, independent of platform."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest
from mcap.reader import make_reader, NonSeekingReader

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'umi_collection/vendor/galaxis/galaxis-core/galaxis-core'))
from galaxis_core.compressed_recorder import CompressedBag
from galaxis_core.mcap_video import VIDEO_TYPE, IMAGE_TYPE, IMAGE_SCHEMA, split_video, validate_packet
from galaxis_core.video_bundle import convert_bundle, cleanup_source
from galaxis_core.recording_readiness import McapReadiness
from galaxis_core.capture_recovery import complete_messages
from test_umi_mp4_bundle import image_message


def capture(root, *, complete=True):
 bag=CompressedBag(root)
 sides=['left','center','right']
 for side in sides:bag.topic(f'/{side}_camera/image_raw/compressed',IMAGE_TYPE,IMAGE_SCHEMA)
 bag.topic('/exact','std_msgs/msg/String',b'string data\n')
 for i in range(16):
  stamp=100_000_000_000+i*33_333_333
  bag.message('/exact',b'\0\1\0\0\x04\0\0\0abc\0',stamp,stamp-1,i)
  for side in sides:bag.message(f'/{side}_camera/image_raw/compressed',image_message(i*33_333_333,'red'),stamp,stamp-2,i)
  time.sleep(.005)
 assert not list(root.glob('*.mcap'))
 assert list(root.glob('*.mcap.partial'))
 bag.close(complete=complete)
 return bag


def test_passive_transport_report_failure_does_not_interrupt_bag_save(tmp_path, monkeypatch):
 import galaxis_core.compressed_recorder as recorder
 bag=CompressedBag(tmp_path/'passive', defer_video=True)
 bag.topic('/exact','std_msgs/msg/String',b'string data\n')
 payload=b'\0\1\0\0\x04\0\0\0abc\0'
 bag.reception.received('/exact',payload,1,1,1)
 bag.message('/exact',payload,1)
 original=recorder.atomic_json
 def failing_report(path,value):
  if path.name=='capture-transport.json':raise OSError('diagnostic only')
  return original(path,value)
 monkeypatch.setattr(recorder,'atomic_json',failing_report)
 bag.close()
 assert (bag.folder/'.deferred-raw.json').exists()
 with bag.partial.open('rb') as f:
  rows=list(make_reader(f,validate_crcs=True).iter_messages())
 assert len(rows)==1 and rows[0][2].data==payload


def test_raw_bag_is_self_contained_and_remux_does_not_reencode_or_delete_it(tmp_path):
 bag=capture(tmp_path/'round')
 marker=json.loads((bag.folder/'.compressed-raw.json').read_text())
 assert marker['sha256']==hashlib.sha256(bag.path.read_bytes()).hexdigest()
 with bag.path.open('rb') as f:rows=list(NonSeekingReader(f,validate_crcs=True).iter_messages(log_time_order=False))
 signals=[m for s,c,m in rows if c.topic=='/exact']
 assert len(signals)==16 and signals[15].sequence==15
 assert all(m.data==b'\0\1\0\0\x04\0\0\0abc\0' for m in signals)
 for side in ('left','center','right'):
  frames=[(s,m) for s,c,m in rows if c.topic==f'/{side}_camera/image_raw/compressed']
  assert len(frames)==16 and all(s.name==VIDEO_TYPE for s,m in frames)
  packets=[split_video(m.data)[1] for s,m in frames]
  assert validate_packet(packets[0],first=True)
  raw=tmp_path/(side+'.h264');raw.write_bytes(b''.join(packets))
  probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-count_frames','-show_entries','stream=nb_read_frames,has_b_frames','-of','json',str(raw)]))['streams'][0]
  assert int(probe['nb_read_frames'])==16 and probe['has_b_frames']==0
 manifest=convert_bundle(bag.folder)
 assert manifest['encoder']['operation']=='copy'
 assert manifest['source_format']=='umi-mcap-h264-v1'
 assert all(c['frame_count']==16 for c in manifest['cameras'])
 cleanup_source(bag.folder,manifest)
 assert bag.path.exists() and (bag.folder/'metadata.yaml').exists()


def test_interrupted_capture_never_publishes_complete_raw_file_and_keeps_recoverable_frames(tmp_path):
 root=tmp_path/'round'
 with pytest.raises(RuntimeError,match='interrupted'):capture(root,complete=False)
 assert not list(root.glob('*.mcap')) and not (root/'.compressed-raw.json').exists()
 partial=next(root.glob('*.mcap.partial'))
 assert list(complete_messages(partial,[]))
 probe=McapReadiness(root,{'/center_camera/image_raw/compressed'},[]);probe.scan()
 assert probe.ready


def test_live_audit_records_a_real_gap_without_discarding_or_stopping_the_round(tmp_path):
 from galaxis_core.capture_recovery import validate_recorded_continuity
 bag=CompressedBag(tmp_path/'gap')
 topic='/center_camera/image_raw/compressed'
 bag.topic(topic,IMAGE_TYPE,IMAGE_SCHEMA)
 for index, offset in enumerate((0,33_333_333,300_000_000,333_333_333)):
  bag.message(topic,image_message(offset,'red'),100_000_000_000+offset,sequence=index)
 bag.close()
 saved=json.loads((bag.folder/'.compressed-raw.json').read_text())
 assert saved['camera_frames'][topic]==4 and saved['continuity_issue'] is None
 assert saved['continuity_summary'][topic]['max_gap_ns']==266_666_667
 assert saved['continuity_summary'][topic]['warnings_over_200ms']==1
 validate_recorded_continuity(bag.folder)
 assert bag.path.exists()


def test_recovered_interval_keeps_keyframe_preroll_but_never_extends_sensor_interval(tmp_path):
 from galaxis_core.capture_recovery import publish_recovered_interval
 bag=capture(tmp_path/'round')
 root=tmp_path/'recovered';root.mkdir()
 start=100_000_000_000+6*33_333_333;end=100_000_000_000+15*33_333_333
 value={'metadata':{'episode':{'start_time':100.,'end_time':101.}},'required_topics':['/exact']}
 publish_recovered_interval(root,[bag.path],value,{'reason':'test'},[],[],[],start,end,
                            {'part_index':1,'part_count':1,'original_episode_folder':'round'})
 with (root/'recovered.mcap').open('rb') as f:rows=list(make_reader(f).iter_messages())
 camera=[m for s,c,m in rows if c.topic=='/center_camera/image_raw/compressed']
 assert camera[0].log_time<start and validate_packet(split_video(camera[0].data)[1],first=True)
 assert all(start<=m.log_time<=end for s,c,m in rows if c.topic=='/exact')
 assert json.loads((root/'meta.json').read_text())['episode']['start_time']==start/1e9


def test_slow_fsync_does_not_hold_message_writer_lock(tmp_path, monkeypatch):
 import threading
 from galaxis_core import compressed_recorder as module
 bag=CompressedBag(tmp_path/'concurrent',defer_video=True)
 bag.topic('/exact','std_msgs/msg/String',b'string data')
 entered,release=threading.Event(),threading.Event()
 real=module.os.fsync
 def blocked(fd):
  entered.set()
  assert release.wait(3)
 monkeypatch.setattr(module.os,'fsync',blocked)
 checker=threading.Thread(target=bag.check);checker.start()
 try:
  assert entered.wait(1)
  assert bag.lock.acquire(timeout=.1)
  bag.lock.release()
  bag.message('/exact',b'original',100)
 finally:
  release.set();checker.join(2);monkeypatch.setattr(module.os,'fsync',real)
 bag.close()
 assert json.loads((bag.folder/'.deferred-raw.json').read_text())['counts']['/exact']==1
