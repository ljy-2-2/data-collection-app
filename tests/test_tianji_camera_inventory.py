"""Partial-camera preview, hotplug and discovery-driven HTTP routing."""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from teleop_control.tianji import TianjiManager, tianji_router

@pytest.fixture
def setup():
    manager=TianjiManager(SimpleNamespace(hardware_enabled=True))
    client=MagicMock();client.__enter__.return_value=client
    client.status.return_value=dict(idle=True,fault=None,max_stream_seconds=600,cooldown_seconds=2)
    client.list_cameras.return_value=[dict(id='usb-1',available=False),dict(id='usb-2',available=True),dict(id='usb-4',available=True)]
    yield manager,client
    manager.camera_client=None;manager.camera_thread=None


def test_only_connected_devices_are_visible_and_removed_frames_are_discarded(setup):
    m,c=setup;m.frames['usb_1']=(b'old',time.monotonic())
    m._prepare_preview(c)
    assert m.camera_preview_mode=='snapshot' and 'usb_1' not in m.frames
    assert [x['id'] for x in m.snapshot()['cameras']]==['usb_2','usb_4']
    c.list_cameras.return_value=[dict(id='extra-5',available=True)]
    m._prepare_preview(c)
    assert [x['id'] for x in m.snapshot()['cameras']]==['extra_5']
    c.list_cameras.return_value=[];m._prepare_preview(c)
    assert m.snapshot()['cameras']==[]


def test_two_wrist_snapshots_are_received_without_starting_three_camera_stream(setup,monkeypatch):
    m,c=setup;monkeypatch.setattr(m,'new_camera_client',lambda:c)
    received=[]
    def read(camera):
        received.append(camera)
        if len(received)==2:m.camera_stop.set()
        return SimpleNamespace(metadata=dict(format='mjpeg'),data=camera.encode())
    c.read.side_effect=read
    original=m._store_camera_frame
    saved={}
    def store(camera,frame):
        original(camera,frame);saved[camera]=m.frames[camera][0]
    monkeypatch.setattr(m,'_store_camera_frame',store)
    m._cameras()
    assert received==['usb-2','usb-4']
    assert saved=={'usb_2':b'usb-2','usb_4':b'usb-4'}
    c.stream.assert_not_called();c.recover_camera.assert_not_called()
    assert not m.camera_error


def test_snapshot_yuyv_is_converted_before_http_preview(setup):
    import numpy as np
    from fusion_camera import Frame
    m,c=setup;m._prepare_preview(c)
    frame=Frame(dict(format='yuyv',width=2,height=2),bytes([80,128,80,128]*2))
    m._store_camera_frame('usb_2',frame)
    assert m.frames['usb_2'][0].startswith(b'\xff\xd8')


def test_new_discovered_camera_http_frame_and_recording_guard(setup):
    m,c=setup;c.list_cameras.return_value=[dict(id='extra-5',available=True)]
    m._prepare_preview(c);m._store_camera_frame('extra_5',SimpleNamespace(metadata=dict(format='mjpeg'),data=b'jpeg'))
    app=FastAPI();app.include_router(tianji_router(m))
    with TestClient(app) as http:
        assert http.get('/tianji/cameras/extra_5/frame').content==b'jpeg'
        assert http.get('/tianji/cameras/not_discovered/frame').status_code==404
    with pytest.raises(RuntimeError,match='连续相机流'):m.adopt_camera_stream()


def test_replugging_complete_group_restores_stream_mode(setup):
    m,c=setup;m._prepare_preview(c)
    assert m.camera_preview_mode=='snapshot'
    c.list_cameras.return_value[0]['available']=True
    m._prepare_preview(c)
    assert m.camera_preview_mode=='stream'
    assert len(m.snapshot()['cameras'])==3


def test_upgraded_controller_streams_only_connected_wrists(setup, monkeypatch):
    from fusion_camera.stream import StreamEvent
    m,c=setup
    c.status.return_value['stream_camera_selection']=True
    monkeypatch.setattr(m,'new_camera_client',lambda:c)
    saved={}
    original=m._store_camera_frame
    def store(camera,frame):
        original(camera,frame);saved[camera]=m.frames[camera][0]
    monkeypatch.setattr(m,'_store_camera_frame',store)
    def events(**kwargs):
        assert kwargs['camera_ids']==['usb-2','usb-4']
        for name in kwargs['camera_ids']:
            yield StreamEvent(dict(type='image',camera_id=name,format='mjpeg'),name.encode())
        m.camera_stop.set()
        yield StreamEvent(dict(type='end',ok=True))
    c.stream.side_effect=events
    m._cameras()
    assert saved=={'usb_2':b'usb-2','usb_4':b'usb-4'}
    c.read.assert_not_called()
    assert not m.camera_error
