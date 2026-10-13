"""HD center-camera relay preserves timestamps, pixels and optional orientation."""
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


def relay(monkeypatch):
    cv2=pytest.importorskip('cv2')
    np=pytest.importorskip('numpy')
    for name in ('rclpy','rclpy.node','rclpy.qos','sensor_msgs','sensor_msgs.msg'):
        monkeypatch.setitem(sys.modules,name,ModuleType(name))
    sys.modules['rclpy.node'].Node=object
    qos=sys.modules['rclpy.qos']
    for key in ('DurabilityPolicy','QoSProfile','ReliabilityPolicy'):setattr(qos,key,object)
    for key in ('CameraInfo','CompressedImage','Image'):setattr(sys.modules['sensor_msgs.msg'],key,SimpleNamespace)
    path=Path(__file__).resolve().parents[1]/'umi_collection/vendor/galaxis/galaxis_bringup/galaxis_bringup/center_camera_flip_node.py'
    spec=importlib.util.spec_from_file_location('_hd_center',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    instance=object.__new__(module.CenterCameraFlipNode)
    instance._vertical_flip=False;instance._jpeg_quality=95
    compressed=[];raw=[]
    instance._compressed_pub=SimpleNamespace(publish=compressed.append)
    instance._image_pub=SimpleNamespace(get_subscription_count=lambda:0,publish=raw.append)
    pixels=np.zeros((120,160,3),dtype=np.uint8)
    pixels[:60,:,2]=200;pixels[60:,:,0]=200
    _,jpeg=cv2.imencode('.jpg',pixels)
    message=SimpleNamespace(data=jpeg.tobytes(),header=SimpleNamespace(stamp=123,frame_id='center'),format='bgr8; jpeg compressed bgr8')
    return instance,message,compressed,raw,cv2,np


def test_unflipped_usb_jpeg_is_forwarded_byte_exact_without_decode(monkeypatch):
    node,message,compressed,raw,cv2,np=relay(monkeypatch)
    monkeypatch.setattr(cv2,'imdecode',lambda *_:pytest.fail('unnecessary JPEG decode'))
    node._on_compressed(message)
    assert compressed==[message] and compressed[0] is message and not raw


def test_flip_and_optional_raw_preview_keep_original_stamp_and_same_orientation(monkeypatch):
    node,message,compressed,raw,cv2,np=relay(monkeypatch)
    node._vertical_flip=True
    node._image_pub.get_subscription_count=lambda:1
    node._on_compressed(message)
    saved=cv2.imdecode(np.frombuffer(compressed[0].data,dtype=np.uint8),cv2.IMREAD_COLOR)
    display=np.frombuffer(raw[0].data,dtype=np.uint8).reshape(120,160,3)
    assert compressed[0].header is message.header and raw[0].header is message.header
    assert raw[0].width==160 and raw[0].height==120
    assert saved[20,20,0]>180 and saved[100,20,2]>180
    assert np.abs(saved.astype(float)-display).mean()<3


def test_optional_raw_preview_does_not_recompress_saved_jpeg(monkeypatch):
    node,message,compressed,raw,cv2,np=relay(monkeypatch)
    node._image_pub.get_subscription_count=lambda:1
    node._on_compressed(message)
    assert compressed[0] is message and raw[0].header is message.header
    assert raw[0].width==160 and raw[0].height==120
