import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from fusion_camera import Frame, FusionCameraClient, CameraError
spec=importlib.util.spec_from_file_location('camera_server',ROOT/'controller/server.py')
server=importlib.util.module_from_spec(spec); spec.loader.exec_module(server)

class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.config=json.loads((ROOT/'controller/server.example.json').read_text())
        self.config['token']='a'*64
        self.service=server.Service(self.config,'/tmp')
    def request(self,**fields):
        return dict(token=self.config['token'],**fields)
    def test_unauthorized_never_touches_usb(self):
        with mock.patch.object(server,'device_info',side_effect=AssertionError('USB touched')):
            result,_=self.service.dispatch(dict(method='snapshot',camera_id='usb-2',token='wrong'))
            self.assertEqual(result['error'],'unauthorized')
    def test_unknown_camera_never_starts_worker(self):
        with mock.patch.object(server.subprocess,'Popen',side_effect=AssertionError('worker')):
            result,_=self.service.dispatch(self.request(method='snapshot',camera_id='../robot'))
            self.assertEqual(result['error'],'unknown_camera')
    def test_global_cooldown_blocks_capture(self):
        self.service.last_finish=server.monotonic()
        with mock.patch.object(server,'device_info',side_effect=AssertionError('USB touched')):
            result,_=self.service.dispatch(self.request(method='snapshot',camera_id='usb-2'))
            self.assertEqual(result['error'],'rate_limited')
            self.assertGreater(result['retry_after'],0)
    def test_fault_stops_automatic_retries(self):
        self.service.fault='worker failed'
        with mock.patch.object(server,'device_info',side_effect=AssertionError('USB touched')):
            result,_=self.service.dispatch(self.request(method='snapshot',camera_id='usb-2'))
            self.assertEqual(result['error'],'capture_paused')
    def test_busy_camera_is_not_claimed(self):
        with mock.patch.object(server,'device_info',return_value=dict(available=True)),mock.patch.object(server,'interface_idle',return_value=False),mock.patch.object(self.service,'capture',side_effect=AssertionError('capture')):
            result,_=self.service.dispatch(self.request(method='snapshot',camera_id='usb-2'))
            self.assertEqual(result['error'],'camera_busy')
    def test_idle_status_does_not_inspect_camera(self):
        with mock.patch.object(server,'device_info',side_effect=AssertionError('USB touched')):
            result,body=self.service.dispatch(self.request(method='status'))
            self.assertTrue(result['idle']);self.assertEqual(body,b'')
    def test_unsafe_config_rejected(self):
        self.config['cameras'][0]['fps']=120
        with self.assertRaises(ValueError):server.validate(self.config)
    def test_yuyv_black_white_decoding_and_shape(self):
        frame=Frame(dict(format='yuyv',width=2,height=1),bytes([16,128,235,128]))
        image=frame.to_pil()
        self.assertEqual(image.size,(2,1))
        self.assertEqual(image.getpixel((0,0)),(0,0,0))
        self.assertEqual(image.getpixel((1,0)),(255,255,255))
        with self.assertRaises(ValueError):Frame(frame.metadata,b'bad').to_pil()
    def test_client_does_not_silently_retry_capture_failure(self):
        client=object.__new__(FusionCameraClient)
        client.timeout=12
        with mock.patch.object(client,'_request',side_effect=CameraError(dict(error='capture_paused'))) as request:
            with self.assertRaises(CameraError):client.read('usb-2')
            self.assertEqual(request.call_count,1)
    def test_read_all_reports_missing_camera_before_capture(self):
        client=object.__new__(FusionCameraClient)
        with mock.patch.object(client,'list_cameras',return_value=[dict(id='usb-1',available=True),dict(id='usb-2',available=False)]),mock.patch.object(client,'read',side_effect=AssertionError('capture started')):
            with self.assertRaises(CameraError) as error:client.read_all()
            self.assertEqual(error.exception.response['error'],'camera_unavailable')

if __name__=='__main__':unittest.main()
