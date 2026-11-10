"""No hardware or network: frame boundaries, persistent config and cloud retries."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from teleop_control.subtask_annotations import AnnotationCycle, SubtaskConfig
from teleop_control.subtask_configs import router
from teleop_control.collection_uploader import CollectionUploader

CONFIG = dict(name='抓放', labels=['pick', 'move', 'place'])
ROOT = Path(__file__).resolve().parents[1]


def test_cycle_boundaries_reset_and_unconfirmed_tail():
    cycle = AnnotationCycle(CONFIG)
    for button, frame in [('X',5),('X',11),('X',14),('X',16),('Y',17),('X',19)]:
        cycle.press(button, frame*1_000_000_000)
    result = cycle.export(i*1_000_000_000 for i in range(22))['subtask_annotation']
    assert [(s['name'],s['start_frame'],s['end_frame']) for s in result['segments']] == [
        ('pick',0,5),('move',5,11),('place',11,14),('pick',14,16),('pick',16,19)]
    assert result['segments'][0]['t_end'] == 4  # Cloud end frame is inclusive.
    assert result['segments'][1]['t_start'] == 5
    assert AnnotationCycle(CONFIG).export(range(10))['subtask_annotation']['segments'] == []
    assert AnnotationCycle().export(range(10)) == {}


def test_empty_presses_do_not_advance_and_missing_frames_are_not_invented():
    cycle=AnnotationCycle(CONFIG)
    for stamp in [0,0,5,5,12]:cycle.press('X',stamp*10**9)
    segments=cycle.export(x*10**9 for x in [0,1,4,7,10,14])['subtask_annotation']['segments']
    assert [(x['name'],x['start_frame'],x['end_frame']) for x in segments]==[('pick',0,3),('move',3,5)]


def test_tianji_uses_clock_offset_and_drift_before_matching_saved_frames():
    cycle=AnnotationCycle(CONFIG);cycle.press('X',5*10**9)
    before=dict(boot_id='one',pc_midpoint_ns=0,offset_ns=30*10**9)
    after=dict(boot_id='one',pc_midpoint_ns=10*10**9,offset_ns=32*10**9)
    segment=cycle.export([30*10**9+i*12*10**8 for i in range(10)],clock_before=before,clock_after=after)['subtask_annotation']['segments'][0]
    assert segment['end_frame']==5
    assert segment['t_end']==4.8
    with pytest.raises(ValueError):cycle.export(range(10),clock_before=before,clock_after={**after,'boot_id':'other'})


@pytest.mark.parametrize('body',[dict(name='',labels=['pick']),dict(name='test',labels=[]),dict(name='test',labels=[' ']),dict(name='test',labels=['bad\nname'])])
def test_config_validation(body):
    with pytest.raises(ValueError):SubtaskConfig(**body)


def test_configuration_library_survives_restart_and_deletes_only_selected(tmp_path):
    path=tmp_path/'configs.json'
    def client():
        app=FastAPI();app.include_router(router(path));return TestClient(app)
    with client() as api:
        first=api.post('/subtask-configs',json=CONFIG).json()
        second=api.post('/subtask-configs',json=dict(name='another',labels=['move','pick'])).json()
        assert api.post('/subtask-configs',json=dict(name='empty',labels=[])).status_code==422
    with client() as api:
        assert api.get('/subtask-configs').json()==[first,second]
        assert api.post('/subtask-configs/'+first['id']+'/delete').is_success
        assert api.get('/subtask-configs').json()==[second]
        assert first['labels']==CONFIG['labels']


def test_cloud_retry_after_ambiguous_label_response_never_duplicates():
    cycle=AnnotationCycle(CONFIG);cycle.press('X',5*10**9);cycle.press('X',11*10**9)
    metadata=cycle.export(i*10**9 for i in range(12))
    labels=[];fail=[True]
    def handler(request):
        if request.method=='GET':return httpx.Response(200,json=dict(items=labels))
        labels.append(json.loads(request.content))
        if fail[0]:
            fail[0]=False
            raise httpx.ReadTimeout('response lost',request=request)
        return httpx.Response(201,json=labels[-1])
    uploader=CollectionUploader.__new__(CollectionUploader)
    uploader.api_url='https://fake'
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        uploader.client=client
        with pytest.raises(httpx.ReadTimeout):uploader._sync_subtask_labels('episode',metadata,dict(author='operator'))
        uploader._sync_subtask_labels('episode',metadata,dict(author='operator'))
        uploader._sync_subtask_labels('episode',metadata,dict(author='operator'))
    assert len(labels)==2
    assert labels[0]==dict(key='subtask',value='pick',t_start=0.,t_end=4.,author='operator')
    assert labels[1]['t_start']==5 and labels[1]['t_end']==10


def test_g2_x_and_y_edges_are_recording_only_and_hold_safe(monkeypatch):
    path=ROOT/'teleop_control/vendor/teleop_runtime/apps/platform_collection.py'
    spec=importlib.util.spec_from_file_location('annotation_g2',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    client=Mock();client.send.return_value=True
    monkeypatch.setattr(module,'_CollectionClient',lambda _:client)
    monkeypatch.setenv('TELEOP_SUBTASK_CONFIG',json.dumps(CONFIG))
    bridge=module.PlatformCollectionBridge('fake',joint_positions={n:0 for names in module.ARM_JOINTS.values() for n in names},gripper_fractions={'left':0,'right':0})
    def frame(a=False,b=False,x=False,y=False,stale=False):
        return SimpleNamespace(right_controller=SimpleNamespace(buttons=SimpleNamespace(primary=a,secondary=b)),left_controller=SimpleNamespace(buttons=SimpleNamespace(primary=x,secondary=y)),stale=stale)
    bridge.process_buttons(frame(x=True));bridge.process_buttons(frame());assert not client.send.called
    bridge.process_buttons(frame(a=True));bridge.process_buttons(frame(x=True));bridge.process_buttons(frame(x=True))
    bridge.process_buttons(frame());bridge.process_buttons(frame(y=True))
    bridge.process_buttons(frame(x=True,stale=True));bridge.process_buttons(frame(x=True));bridge.process_buttons(frame())
    bridge.process_buttons(frame(x=True,b=True))
    assert [(call.args[0],call.kwargs.get('button')) for call in client.send.call_args_list]==[('start',None),('annotation','X'),('annotation','Y'),('stop',None)]


def test_kuavo_records_physical_y_but_masks_robot_return_when_annotating():
    from dataclasses import dataclass
    from teleop_control.kuavo_input import InputAdapter, ActivationGate
    @dataclass(frozen=True)
    class Pose:
        position_m: tuple = (0.,0.,0.)
    @dataclass(frozen=True)
    class Buttons:
        primary: bool = True
        secondary: bool = True
    @dataclass(frozen=True)
    class Hand:
        pose: Pose = Pose()
        buttons: Buttons = Buttons()
        axis: tuple = (0.,0.)
        grip: float = 0.
        trigger: float = 0.
    @dataclass(frozen=True)
    class Frame:
        head_pose: Pose = Pose()
        left_controller: Hand = Hand()
        right_controller: Hand = Hand()
        stale: bool = False
    frame=Frame();observation=Mock();activation=ActivationGate()
    activation.annotation=True
    adapter=InputAdapter(SimpleNamespace(read_frame=lambda:frame),observation=observation,activation=activation)
    output=adapter.read_frame()
    assert observation.collection_input.call_args.args[0].left_controller.buttons.secondary
    assert not output.left_controller.buttons.secondary
    assert not output.left_controller.buttons.primary
    assert output.right_controller.buttons==frame.right_controller.buttons
    activation.annotation=False
    assert adapter.read_frame().left_controller.buttons.secondary


def test_kuavo_annotation_reserves_x_without_overwriting_saved_start_setting(tmp_path,monkeypatch):
    from teleop_control.kuavo_collection import KuavoCollection
    monkeypatch.setattr('threading.Thread.start',lambda _:None)
    recorder=KuavoCollection(SimpleNamespace(),tmp_path/'episodes')
    recorder.settings['start_button']='X'
    recorder.arm(dict(subtask_config=CONFIG))
    assert recorder.start_button()=='A'
    assert recorder.snapshot()['settings']['start_button']=='A'
    assert recorder.settings['start_button']=='X'
    recorder.arm({})
    assert recorder.start_button()=='X'


def test_g2_collector_ipc_retains_annotations_until_save_and_resets_next_episode():
    import io
    import queue
    import threading
    from unittest.mock import MagicMock
    spec=importlib.util.spec_from_file_location('annotation_remote',ROOT/'teleop_control/vendor/remote_collector.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    server=module.ControlServer.__new__(module.ControlServer)
    server._lock=threading.Lock();server.stop_event=threading.Event();server.shutdown_event=threading.Event()
    server.annotation_config=CONFIG;server._publish_recording=Mock();server.starts=queue.Queue()
    server.begin_episode()
    messages=[dict(type='annotation',button='X',pc_ts_ns=5*10**9),
              dict(type='annotation',button='Y',pc_ts_ns=6*10**9),
              dict(type='annotation',button='X',pc_ts_ns=11*10**9),
              dict(type='stop'),dict(type='annotation',button='X',pc_ts_ns=12*10**9),dict(type='shutdown')]
    connection=MagicMock();connection.makefile.return_value=io.StringIO('\n'.join(json.dumps(x) for x in messages))
    server.sock=SimpleNamespace(accept=lambda:(connection,None))
    server._run();server.finish_episode()
    segments=server.annotations.export(i*10**9 for i in range(13))['subtask_annotation']['segments']
    assert [(x['name'],x['start_frame'],x['end_frame']) for x in segments]==[('pick',0,5),('pick',5,11)]
    server.begin_episode()
    assert not server.annotations.events
