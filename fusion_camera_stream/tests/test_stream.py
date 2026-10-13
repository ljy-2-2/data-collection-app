import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'controller')]
from joints import parse, JointReader
from fusion_camera.episode import match_bundles, encode_image, map_gripper_clocks
from fusion_camera.stream import FusionStreamClient,StreamEvent
from fusion_camera.grippers import query_bytes, ReadOnlyGrippers
from PIL import Image

class StreamTests(unittest.TestCase):
    def test_dual_arm_offsets_and_units(self):
        data=bytearray(1430);data[:2]=b'FX'
        for arm in range(2):
            b=762+312*arm
            struct.pack_into('<i',data,b,123)
            struct.pack_into('<7f',data,b+4,*[float(arm*10+j) for j in range(7)])
            struct.pack_into('<7f',data,b+32,*[.1]*7)
            struct.pack_into('<7f',data,b+144,*[3.]*7)
        state=parse(data)
        self.assertEqual(state['position_deg'],[0.,1.,2.,3.,4.,5.,6.,10.,11.,12.,13.,14.,15.,16.])
        self.assertEqual(state['torque_nm'],[3.]*14)
        struct.pack_into('<f',data,766,float('nan'))
        with self.assertRaises(ValueError):parse(data)

    def test_readonly_mapping_flags(self):
        import joints,os,mmap
        memory=mock.MagicMock()
        memory.__getitem__.return_value=struct.pack('<I',4096)
        with mock.patch.object(joints.os,'open',return_value=42) as opened, mock.patch.object(joints.os,'close'), mock.patch.object(joints.mmap,'mmap',return_value=memory) as mapped, mock.patch.object(JointReader,'sample'):
            r=JointReader(lambda:1);r.close()
        opened.assert_called_once_with('/dev/FXDcss',os.O_RDONLY)
        mapped.assert_called_once_with(42,4096,flags=mmap.MAP_SHARED,prot=mmap.PROT_READ)

    def test_nearest_matching_preserves_real_times_no_duplicates(self):
        joint={'joint_names':['j']*14,'position_deg':[0]*14,'velocity_deg_s':[0]*14,'torque_nm':[0]*14,'pc_recv_ns':999,'frame_serial':[1,1]}
        js=[dict(joint,timestamp_ns=t,sequence=i,tick=i) for i,t in enumerate([100,130,160])]
        cams={c:[{'timestamp_ns':t} for t in ts] for c,ts in {'a':[101,161],'b':[102,162],'c':[99,159]}.items()}
        rows=match_bundles(js,cams,tolerance_ns=5)
        self.assertEqual([r['timestamp_ns'] for r in rows],[100,160])
        self.assertEqual([r['camera_frame_index']['a'] for r in rows],[0,1])
        self.assertEqual(rows[0]['cam_ts']['a'],101)

    def test_jpeg_is_really_decoded_resized_and_native_preserved(self):
        buf=io.BytesIO();Image.new('RGB',(1024,768),(123,45,67)).save(buf,format='JPEG')
        data=buf.getvalue()
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            for sub in ['frames','native']:(p/'camera/usb_1_color'/sub).mkdir(parents=True)
            event=StreamEvent({'camera_id':'usb-1','width':1024,'height':768},data)
            encode_image(p,event,0)
            self.assertEqual((p/'camera/usb_1_color/native/00000000.jpg').read_bytes(),data)
            with Image.open(p/'camera/usb_1_color/frames/00000000.jpg') as im:
                im.load();self.assertEqual(im.size,(640,480))

    def test_invalid_duration_does_not_connect(self):
        client=object.__new__(FusionStreamClient)
        with mock.patch('socket.create_connection',side_effect=AssertionError('network touched')):
            with self.assertRaises(ValueError):next(client.stream(seconds=601))

    def test_close_stops_active_stream_socket(self):
        client=object.__new__(FusionStreamClient)
        sock=mock.Mock();client._stream_socket=sock
        client.close()
        sock.sendall.assert_called_once_with(b'S')
        sock.close.assert_called_once()
        self.assertTrue(client.closed)
        self.assertIsNone(client._stream_socket)

    def test_gripper_transport_only_constructs_whitelisted_reads(self):
        for r in (0x50,0x51,0x3D,0x3E):
            self.assertEqual(query_bytes(1,r),struct.pack('<IHBB',0x7ff,1,0x33,r))
        for r in (0x0A,0x09,0xFC,0xFD,0xFE,0x55):
            with self.assertRaises(ValueError):query_bytes(1,r)
        with self.assertRaises(ValueError):query_bytes(2,0x50)

    def test_unsolicited_gripper_reply_aborts_without_more_queries(self):
        r=object.__new__(ReadOnlyGrippers)
        r.closed=False;r.trace=[];r.unexpected_reply=None;r.duplicate_replies=0
        r.config={'motors':{'left':{'motor_id':1}}}
        send=mock.Mock(return_value=True)
        raw=bytes.fromhex('000000000100330000007041')
        def receive(buf,channel):
            buf[:len(raw)]=raw
            return len(raw)
        r.senders={'left':send};r.receivers={'left':receive}
        with self.assertRaisesRegex(RuntimeError,'ownership/freshness unverified'):
            r.read_register('left',0x51)
        send.assert_called_once()
        self.assertEqual(r.unexpected_reply['raw_hex'],raw.hex())
        self.assertEqual(r.trace,[{'side':'left','can_id':0x7ff,'function':0x33,'register':0x51}])

    def test_triplet_midpoint_uses_higher_rate_joint_samples(self):
        joint={'joint_names':['j']*14,'position_deg':[0]*14,'velocity_deg_s':[0]*14,'torque_nm':[0]*14,'pc_recv_ns':999,'frame_serial':[1,1]}
        js=[dict(joint,timestamp_ns=t,sequence=i,tick=i) for i,t in enumerate(range(0,101,10))]
        cams={k:[{'timestamp_ns':t}] for k,t in [('a',41),('b',55),('c',60)]}
        row=match_bundles(js,cams,tolerance_ns=25)[0]
        self.assertEqual(row['timestamp_ns'],50)
        self.assertEqual(row['joint_per_camera']['a']['joint_timestamp_ns'],40)
        self.assertEqual(row['joint_per_camera']['c']['joint_timestamp_ns'],60)

    def test_gripper_affine_clock_mapping_preserves_raw_clocks(self):
        sample={'motors':{s:{'fields':{'position_rad':{'pc_query_start_ns':480,'pc_reply_ns':520}}} for s in ('left','right')}}
        a={'boot_id':'boot','pc_midpoint_ns':0,'offset_ns':1000,'uncertainty_ns':2}
        b={'boot_id':'boot','pc_midpoint_ns':1000,'offset_ns':1100,'uncertainty_ns':3}
        map_gripper_clocks([sample],a,b)
        self.assertEqual(sample['timestamp_ns'],1550)
        self.assertEqual(sample['motors']['left']['fields']['position_rad']['pc_reply_ns'],520)
        self.assertEqual(sample['motors']['left']['fields']['position_rad']['timestamp_uncertainty_estimate_ns'],23)

if __name__=='__main__':unittest.main()
