import argparse
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'fusion_camera_stream'), str(ROOT/'fusion_data_collection'),
                str(ROOT/'fusion_data_collection/teleop_patch/marvin_teleop')]
from FusionDataCollector.collector import Buttons, Episode, readiness, camera_mapping, attach_feedback
from collection_telemetry import Publisher
from fusion_camera.stream import StreamEvent, FusionStreamClient
from PIL import Image


def telemetry(stamp=None, seq=1, a=0, b=0):
    stamp = stamp or time.monotonic_ns()
    return {'schema':'fusion_collection_telemetry_v1','mode':'live','session':'test-session',
        'vr_session':1,'sequence':seq,'pc_monotonic_ns':stamp,'pc_unix_ns':time.time_ns(),
        'vr_connected':True,'buttons':{'A':False,'B':False},'button_press_counts':{'A':a,'B':b},
        'joints_deg':{s:[0]*7 for s in ('left','right')},
        'joint_feedback':{s:{'frame':seq,'unchanged_s':0} for s in ('left','right')},
        'grippers':{'fault':'','motors':{s:{'display_endpoints_rad':{'open_rad':1.,'closed_rad':0.},
            'feedback':{'received':stamp/1e9,'position':.5,'velocity':0,'torque':0,'state':1}}
            for s in ('left','right')}}}


class CollectionTests(unittest.TestCase):
    def test_buttons_no_start_on_connect_hold_b_priority_repeated_round(self):
        b = Buttons()
        self.assertIsNone(b.accept(telemetry(a=1)))
        self.assertIsNone(b.accept(telemetry(a=1)))
        self.assertEqual(b.accept(telemetry(a=2)),'start')
        self.assertEqual(b.accept(telemetry(a=3,b=1)),'stop')
        self.assertIsNone(b.accept(telemetry(a=3,b=1)))
        self.assertEqual(b.accept(telemetry(a=4,b=1)),'start')
        r = telemetry(a=5);r['session']='reconnected'
        self.assertIsNone(b.accept(r))

    def test_delayed_feedback_allowed_but_missing_and_simulated_feedback_rejected(self):
        r = telemetry();self.assertIsNone(readiness(r))
        r['mode']='simulation';self.assertIsNotNone(readiness(r))
        r=telemetry();r['grippers']=None;self.assertIsNotNone(readiness(r))
        r=telemetry();r['grippers']['motors']['right']['feedback']['received']-=1
        self.assertIsNone(readiness(r))
        r=telemetry();r['buttons']['A']=None;self.assertIsNotNone(readiness(r))

    def test_mapping_does_not_allow_path_traversal_or_duplicates(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'map.json'
            for m in ({'usb-1':'../../root','usb-2':'right','usb-4':'head'},
                      {'usb-1':'left','usb-2':'left','usb-4':'head'}):
                p.write_text(json.dumps(m))
                with self.assertRaises(ValueError):camera_mapping(p)

    def test_gripper_clock_conversion_no_fake_missing_angle(self):
        b=[{'timestamp_ns':1550,'tensors':{}}]
        g={s:[{'pc_feedback_received_monotonic_ns':500,'motor_position_rad':3.,'opening_angle_rad':None}]
           for s in ('left','right')}
        attach_feedback(b,g,{'boot_id':'b','pc_midpoint_ns':0,'offset_ns':1000,'uncertainty_ns':2},
            {'boot_id':'b','pc_midpoint_ns':1000,'offset_ns':1100,'uncertainty_ns':3})
        self.assertEqual(g['left'][0]['timestamp_ns'],1550)
        self.assertEqual(b[0]['tensors']['state__left_effector__position'],[3.])
        self.assertEqual(b[0]['tensors']['state__left_effector__opening_angle'],[None])

    def test_graceful_request_keeps_socket_readable(self):
        from unittest.mock import Mock
        c=object.__new__(FusionStreamClient);c._stream_socket=Mock()
        c.request_stop();c.request_stop()
        c._stream_socket.sendall.assert_called_once_with(b'S')
        c._stream_socket.shutdown.assert_not_called()
        c._stream_socket.close.assert_not_called()

    def test_full_episode_b_stop_real_jpeg_all_16_state_values(self):
        base = time.monotonic_ns()
        offset = 3000000000
        buf=io.BytesIO();Image.new('RGB',(1024,768),(60,100,150)).save(buf,format='JPEG')
        jpeg=buf.getvalue()
        holder={}
        class Fake:
            stopped=False
            def status(self):
                return {'idle':True,'observation_modes':['teleop_observation'],'graceful_stop':True}
            def list_cameras(self):
                return [{'id':c,'available':True} for c in ('usb-1','usb-2','usb-4')]
            def clock_sync(self,samples=3):
                return {'boot_id':'offline','offset_ns':offset,'uncertainty_ns':100,
                        'pc_midpoint_ns':base if not self.stopped else base+2000000000}
            def request_stop(self):self.stopped=True
            def close(self):pass
            def stream(self,**kw):
                self.assert_mode=kw['mode']
                yield StreamEvent({'type':'start','boot_id':'offline','observation_mode':'teleop_observation','graceful_stop':True})
                images=[0,0,0];joints=0
                for i in range(100):
                    stamp=base+i*10000000
                    row=telemetry(stamp,seq=i+2)
                    for m in row['grippers']['motors'].values():
                        m['feedback']['received']=(base+(i//2)*20000000)/1e9
                    holder['ep'].submit(row)
                    if i==90:holder['ep'].stop('B')
                    yield StreamEvent({'type':'joints','position_deg':list(range(14)),
                        'velocity_deg_s':[0]*14,'torque_nm':[1]*14,
                        'joint_names':[s+'_joint_'+str(j) for s in ('left','right') for j in range(1,8)],
                        'timestamp_ns':stamp+offset,'sequence':i,'tick':i,'frame_serial':[i,i],
                        'pc_recv_ns':time.time_ns()})
                    joints+=1
                    if self.stopped:break
                    if i%10 in (0,3,7):
                        for k,c in enumerate(('usb-1','usb-2','usb-4')):
                            yield StreamEvent({'type':'image','camera_id':c,'width':1024,'height':768,
                                'timestamp_ns':stamp+offset+k*1000000,'sequence':images[k]},jpeg)
                            images[k]+=1
                    time.sleep(.003)
                yield StreamEvent({'type':'end','ok':True,'joint_samples':joints,'frames':images})
        with tempfile.TemporaryDirectory() as d:
            args=argparse.Namespace(output=d,max_seconds=2,task='OFFLINE_SYNTHETIC_TEST',retain_native=False)
            ep=Episode(args,{'usb-1':'hand_left_color','usb-2':'hand_right_color','usb-4':'head_color'},
                       telemetry(base),client_factory=Fake)
            holder['ep']=ep
            with patch('builtins.print'):ep.run()
            self.assertIsNone(ep.error)
            meta=json.loads((ep.directory/'meta_info.json').read_text())
            self.assertTrue(meta['complete']);self.assertEqual(meta['stop_reason'],'B')
            rows=[json.loads(s) for s in (ep.directory/'record/bundles.jsonl').read_text().splitlines()]
            self.assertGreater(len(rows),20)
            self.assertEqual(len(rows[0]['tensors']['state__joint__position']),14)
            for side in ('left','right'):
                self.assertEqual(len(rows[0]['tensors']['state__'+side+'_arm__position']),7)
                self.assertEqual(rows[0]['tensors']['state__'+side+'_effector__position'],[.5])
                gs=[json.loads(s) for s in (ep.directory/('record/'+side+'_gripper.jsonl')).read_text().splitlines()]
                self.assertLess(len(gs),60)  # Never mislabel 100 Hz cached repeats as fresh.
            for name in ('hand_left_color','hand_right_color','head_color'):
                for path in (ep.directory/'camera'/name/'frames').glob('*.jpg'):
                    with Image.open(path) as image:
                        image.load();self.assertEqual(image.size,(640,480))

    def test_overflow_or_disconnect_stops_only_recorder(self):
        r=telemetry()
        ep=Episode(None,{'usb-1':'head_color'},r)
        for i in range(1100):ep.submit(dict(r,sequence=i+2))
        self.assertTrue(ep.stopping.is_set());self.assertIsNotNone(ep.error)

if __name__=='__main__':unittest.main()
