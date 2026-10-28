"""All producers and transports are fake; never discover hardware."""
import json
import math
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from teleop_control.kuavo_collection import KuavoCollection, CollectionSettings, ROLES
from teleop_control.kuavo_preview import ARM_NAMES
from teleop_control.collection_uploader import READY_MARKER

class Feed:
    def __init__(self):
        self.lock=threading.Lock();self.sequence=0;self.received=time.monotonic();self.width=640;self.height=480
        self.frame=b'\xff\xd8fake\xff\xd9';self.source_stamp=123
    def snapshot(self):return dict(connected=True)

def recorder(tmp_path):
    cameras=SimpleNamespace(lock=threading.RLock(),feeds={r:Feed() for r in ROLES})
    return KuavoCollection(SimpleNamespace(cameras=cameras),tmp_path/'episodes')

def wait(predicate):
    deadline=time.monotonic()+3
    while not predicate():
        assert time.monotonic()<deadline,'worker timeout'
        time.sleep(.005)

def event(kind,**kw):return dict(kind=kind,pc_monotonic_ns=time.monotonic_ns(),**kw)

def button(**kw):return event('input',fresh=True,enabled=True,buttons=dict(A=kw.get('A',False),B=kw.get('B',False),X=kw.get('X',False),Y=kw.get('Y',False)))

def packet():
    return event('packet',packet=dict(event='control_trace',joint_order=list(ARM_NAMES),feedback_rad=[.1]*14,
                command_rad=[.2]*14,feedback_timestamp_ns=time.monotonic_ns(),publication_completed_monotonic_ns=111,
                diagnostic_sampling_hz=10,diagnostic_samples_omitted_before=9))

def joints(count=20):
    now=time.monotonic_ns()
    return [dict(kind='joint_sample',pc_monotonic_ns=now,source_timestamp_ns=1_000_000_000+i*10_000_000,
        source_sequence=i,joint_names=list(ARM_NAMES),position_rad=[.3]*14,velocity_rad_s=[.1]*14) for i in range(count)]


def motors():
    return event('packet',packet=dict(event='status',grippers=dict(mode='injected_driver',motors={side:dict(state='READY',
        open_rad=-2.,closed_rad=-1.,target_rad=None,feedback_age_ms=0.,feedback=dict(position_rad=-1.5,received_ns=time.monotonic_ns())) for side in ('left','right')})))


def frame(c):
    now=time.monotonic()
    for f in c.manager.cameras.feeds.values():
        with f.lock:f.sequence+=1;f.received=now

def test_ab_episode_native_rate_schema_and_destination(tmp_path,monkeypatch):
    import teleop_control.kuavo_collection as module
    monkeypatch.setattr(module,'collection_available',lambda:True)
    context=dict(author='operator',upload_destination={'kind':'local','api_url':'http://fake'})
    monkeypatch.setattr(module,'collection_context',lambda:context)
    c=recorder(tmp_path);c.arm(dict(task='test',arm_mode='left'))
    try:
        # Held A on entering a session never starts a recording.
        c.accept([*joints(),packet(),button(A=True)]);time.sleep(.03);assert c.phase=='ready'
        c.accept([button(),button(A=True)])
        wait(lambda:c.phase=='recording');frame(c);c.accept([*joints(),packet(),motors()]);wait(lambda:c.frames==1)
        c.accept([button(),button(Y=True)])
        time.sleep(.03);assert c.phase=='recording'  # Y stays return, never save.
        c.accept([button(),button(B=True,A=True)])
        wait(lambda:c.phase=='saved')
        directory=c.output/next(c.output.iterdir()).name
        meta=json.loads((directory/'meta_info.json').read_text());marker=json.loads((directory/READY_MARKER).read_text())
        assert meta['complete'] and meta['full_rate_dataset'] is False and meta['controlled_sides']==['left']
        assert marker['upload_destination']==context['upload_destination'] and marker['author']=='operator'
        action=json.loads((directory/'record/actions.jsonl').read_text().splitlines()[0]);assert action['position_rad']==[.2]*14
        assert action['diagnostic_samples_omitted_before']==9
        bundle=json.loads((directory/'record/bundles.jsonl').read_text().splitlines()[0])
        assert bundle['tensors']['state__joint__position']==[.3]*14
        assert bundle['tensors']['state__left_effector__position']==[-1.5]
        assert bundle['tensors']['action__left_effector__position']==[-1.5]
        assert bundle['gripper_target_sources']['left']['source']=='initial_feedback'
        assert not bundle['hardware_synchronized']
        for role in ROLES:assert (directory/'camera'/(role+'_color')/'frames/00000000.jpg').is_file()
    finally:c.close()

@pytest.mark.parametrize('fault',['stop','overflow','camera','session_fault'])
def test_partial_observation_is_saved_and_never_stops_cameras(tmp_path,monkeypatch,fault):
    monkeypatch.delenv('DATA_COLLECTION_GATE_FILE',raising=False)
    c=recorder(tmp_path);c.arm(dict(task='test'))
    c.accept([*joints(),packet(),button(),button(A=True)]);wait(lambda:c.phase=='recording')
    frame(c);c.accept([*joints(),packet(),motors()]);wait(lambda:c.frames==1)
    if fault=='overflow':c.accept([],True)
    elif fault=='camera':c.manager.cameras.feeds={}
    elif fault=='session_fault':c.accept([dict(kind='fault',error='robot transport disconnected')])
    if fault=='stop':c.disarm()
    else:
        time.sleep(.04);assert c.phase=='recording'
        c.accept([button(),button(B=True)])
    wait(lambda:c.phase=='saved');c.close()
    meta=json.loads(next(c.output.rglob('meta_info.json')).read_text())
    assert meta['complete'] and list(c.output.rglob(READY_MARKER))
    if fault=='stop':assert meta['closure_reason']=='session_end'
    else:assert meta['quality_warnings']


def test_config_validation_and_persistence(tmp_path):
    c=recorder(tmp_path);c.configure(CollectionSettings(start_button='X'))
    assert recorder(tmp_path).settings['start_button']=='X'
    for value in ({'save_button':'Y'},{'start_button':'Y'},{'enabled':'yes'}):
        with pytest.raises(ValueError):CollectionSettings(**value)
    c.armed=True
    with pytest.raises(ValueError):c.configure(CollectionSettings())


def test_recording_waits_through_input_joint_and_camera_outages(tmp_path,monkeypatch):
    import teleop_control.kuavo_collection as module
    monkeypatch.setattr(module,'collection_available',lambda:True)
    monkeypatch.setattr(module,'collection_context',lambda:{})
    c=recorder(tmp_path);c.manager.cameras.feeds={};c.arm({})
    try:
        c.accept([button(),button(A=True)])
        wait(lambda:c.phase=='recording')
        # No source at startup, then gaps longer than all former watchdogs.
        time.sleep(1.15);assert c.phase=='recording'
        c.accept(joints())
        time.sleep(.3);assert c.phase=='recording'
        c.accept([button(),button(B=True)])
        wait(lambda:c.phase=='saved')
        meta=json.loads((Path(c.last_episode)/'meta_info.json').read_text())
        assert meta['joint_samples']==20 and not meta['joint_quality_passed']
        assert meta['complete'] and not any(v['frames'] for v in meta['cameras'].values())
    finally:c.close()


@pytest.mark.parametrize('destination',['local','cloud'])
@pytest.mark.parametrize('stationary',[False,True])
def test_complete_episode_uses_shared_upload_contract(tmp_path,monkeypatch,destination,stationary):
    import httpx
    from teleop_control.collection_uploader import CollectionUploader, UPLOADED_MARKER
    monkeypatch.delenv('DATA_COLLECTION_GATE_FILE',raising=False)
    c=recorder(tmp_path);c.options={'task':'synthetic','arm_mode':'dual'};c.session='fake-session'
    ep=c._begin({'author':'test','upload_destination':{'id':destination}})
    frame(c);c._frames(ep);ep['events']=([packet()] if not stationary else [])+[motors()];ep['joint_samples']=joints();c._finish(ep,True)
    assert c.phase=='saved'
    directory=ep['directory'];seen=[]
    def handle(request):
        seen.append(request)
        if request.url.host=='fake-storage':
            assert 'Authorization' not in request.headers
            assert int(request.headers['Content-Length'])==len(request.content)
            return httpx.Response(200)
        if request.url.path=='/uploads/presign':
            body=json.loads(request.content);name=body['filenames'][0]
            return httpx.Response(200,json={'urls':[dict(filename=name,put_url='https://fake-storage/'+name,key='raw/fake/'+name)]})
        if request.url.path=='/episodes':return httpx.Response(201,json={'id':'fake'})
        raise AssertionError(str(request.url))
    client=httpx.Client(transport=httpx.MockTransport(handle),auth=httpx.BasicAuth('fake','fake'))
    uploader=CollectionUploader(c.output,'https://'+destination+'.test',client=client)
    uploader._upload_episode(directory,json.loads((directory/READY_MARKER).read_text()))
    assert (directory/UPLOADED_MARKER).exists()
    payload=json.loads(next(r.content for r in seen if r.url.path=='/episodes'))
    assert payload['extra']['robot_type']=='kuavo'
    assert payload['extra']['source_features']['action']['names']==list(ARM_NAMES)
    assert payload['extra']['timing']['sample_clock']=='pc_monotonic_ns'
    assert not payload['extra']['timing']['full_rate_dataset']
    assert payload['extra']['actions_recorded'] is not stationary
    assert payload['extra']['source_features']['action']['nullable'] is True
    assert payload['extra']['source_features']['action']['validity_field']=='action_observed'
    if stationary:
        assert (directory/'record/actions.jsonl').read_text()==''
        bundle=json.loads((directory/'record/bundles.jsonl').read_text().splitlines()[0])
        assert not bundle['action_observed'] and bundle['action_delta_ms'] is None
        assert bundle['tensors']['action__joint__position']==[None]*14
        assert bundle['tensors']['state__joint__position']==[.3]*14
    assert payload['started_at'] and payload['ended_at']
    client.close()


def test_missing_gripper_is_null_and_does_not_prevent_save(tmp_path):
    c=recorder(tmp_path);c.options={'task':'synthetic'};c.session='fake'
    ep=c._begin({});frame(c);c._frames(ep);ep['events']=[packet()];ep['joint_samples']=joints();c._finish(ep,True)
    assert c.phase=='saved' and not c.error
    assert (ep['directory']/READY_MARKER).exists()
    row=json.loads((ep['directory']/'record/bundles.jsonl').read_text().splitlines()[0])
    assert row['tensors']['state__right_effector__position']==[None]


def test_idle_frames_before_and_after_command_are_preserved(tmp_path,monkeypatch):
    c=recorder(tmp_path);c.options={};c.session='mixed'
    ep=c._begin({});end=time.monotonic_ns();ep['start_ns']=end-1_000_000_000
    ep['joint_samples']=joints(100)
    for i,row in enumerate(ep['joint_samples']):row['pc_monotonic_ns']=ep['start_ns']+i*10_000_000
    for i,offset in enumerate((900_000_000,500_000_000,100_000_000)):
        stamp=end-offset
        for feed in c.manager.cameras.feeds.values():
            feed.sequence=i+1;feed.received=stamp/1e9
        c._frames(ep)
        motor=motors();motor['pc_monotonic_ns']=stamp;ep['events'].append(motor)
        if i==1:
            action=packet();action['pc_monotonic_ns']=stamp;ep['events'].append(action)
    monkeypatch.setattr(time,'monotonic_ns',lambda:end)
    c._finish(ep,True)
    assert c.phase=='saved'
    rows=[json.loads(line) for line in (ep['directory']/'record/bundles.jsonl').read_text().splitlines()]
    assert len(rows)==3 and [r['action_observed'] for r in rows]==[False,True,False]
    assert rows[0]['tensors']['action__joint__position']==[None]*14
    assert rows[1]['tensors']['action__joint__position']==[.2]*14
    assert rows[2]['tensors']['action__joint__position']==[None]*14


@pytest.mark.parametrize('damage',['legacy','tianji','joints','actions','corrupt','count'])
def test_optional_commands_never_bypass_other_upload_requirements(tmp_path,damage):
    import httpx
    from teleop_control.collection_uploader import CollectionUploader
    c=recorder(tmp_path);c.options={};c.session='stationary'
    ep=c._begin({});frame(c);c._frames(ep);ep['events']=[motors()];ep['joint_samples']=joints();c._finish(ep,True)
    directory=ep['directory'];path=directory/'meta_info.json';meta=json.loads(path.read_text())
    assert meta['complete']
    if damage=='legacy':meta['schema_version']=2
    elif damage=='tianji':meta['robot_type']='tianji'
    elif damage in ('joints','actions'):(directory/'record'/(damage+'.jsonl')).unlink()
    elif damage=='corrupt':meta['recording_integrity']['errors']=['invalid timestamp']
    else:meta['action_samples']=1
    path.write_text(json.dumps(meta))
    def no_upload(request):raise AssertionError('invalid episode reached upload')
    with httpx.Client(transport=httpx.MockTransport(no_upload)) as client:
        uploader=CollectionUploader(c.output,'https://local.test',client=client)
        with pytest.raises(ValueError):uploader._upload_episode(directory,json.loads((directory/READY_MARKER).read_text()))


def test_a_starts_before_motion_and_b_saves_after_real_motion(tmp_path,monkeypatch):
    monkeypatch.delenv('DATA_COLLECTION_GATE_FILE',raising=False)
    c=recorder(tmp_path);c.arm(dict(task='start-before-grip'))
    try:
        c.accept([*joints(),button(),button(A=True)])
        wait(lambda:c.phase=='recording')
        frame(c);c.accept([*joints(),packet(),motors()]);wait(lambda:c.frames==1)
        c.accept([button(),button(B=True)]);wait(lambda:c.phase=='saved')
        assert c.completed==1 and not c.error
    finally:c.close()


def test_idle_grip_does_not_end_recording_or_invent_actions(tmp_path,monkeypatch):
    monkeypatch.delenv('DATA_COLLECTION_GATE_FILE',raising=False)
    c=recorder(tmp_path);c.arm(dict(task='stationary-observation'))
    try:
        c.accept([*joints(),button(),button(A=True)])
        wait(lambda:c.phase=='recording')
        # Longer than the removed command timeout; all observation producers
        # remain fresh, but no arm commands have occurred.
        deadline=time.monotonic()+1.15;sequence=0
        while time.monotonic()<deadline:
            samples=joints()
            for sample in samples:
                sample.update(source_sequence=sequence,source_timestamp_ns=1_000_000_000+sequence*10_000_000)
                sequence+=1
            frame(c);c.accept([*samples,button(),motors()]);time.sleep(.02)
            assert c.phase=='recording'
        c.accept([button(B=True)]);wait(lambda:c.phase=='saved')
        directory=Path(c.last_episode)
        assert not c.error
        assert (directory/'record/actions.jsonl').read_text()==''
        assert (directory/READY_MARKER).exists()
        meta=json.loads((directory/'meta_info.json').read_text())
        assert meta['complete'] and not meta['actions_recorded'] and meta['action_samples']==0
        assert meta['frames_recorded']>0 and meta['joint_samples']>0
    finally:c.close()


def test_b_without_a_gives_feedback_and_preserves_login_failure(tmp_path,monkeypatch):
    monkeypatch.setattr('teleop_control.kuavo_collection.collection_available',lambda:False)
    monkeypatch.delenv('DATA_COLLECTION_GATE_FILE',raising=False)
    c=recorder(tmp_path);c.arm(dict(task='button-feedback'))
    try:
        c.accept([button(),button(B=True)])
        wait(lambda:'尚未开始采集' in c.error)
        assert c.last_episode is None
        c.accept([button(),button(A=True)])
        wait(lambda:'请先登录' in c.error)
        error=c.error
        c.accept([button(),button(B=True)]);time.sleep(.03)
        assert c.error==error and c.last_episode is None
    finally:c.close()


@pytest.mark.parametrize('stationary_side',['left','right'])
def test_full_batch_episode_has_real_100hz_feedback_and_32_position_curves(tmp_path,stationary_side):
    from test_kuavo_recording import stream, BASE, OFFSET
    c=recorder(tmp_path);c.options=dict(arm_mode='dual');c.session='fake'
    ep=c._begin({});ep['start_ns']=BASE+OFFSET+1;ep['stop_ns']=BASE+OFFSET+1_000_000_000
    ep['stop_unix_ns']=ep['start_unix_ns']+1_000_000_000
    ep['events']=stream()
    for row in ep['events']:
        for side,samples in row['packet']['samples'].items():
            for sample in samples:
                if side==stationary_side:
                    sample.update(position_rad=-1.5,recording_target=None)
                else:
                    sample['recording_target']=dict(position_rad=-1.3,source='owner_acknowledged_target',
                        source_timestamp_ns=sample['received_ns']-1_000_000)
    ep['joint_samples']=[dict(pc_monotonic_ns=BASE+OFFSET+(i+1)*10_000_000,
        source_timestamp_ns=BASE+(i+1)*10_000_000,source_sequence=i,
        joint_names=list(ARM_NAMES),position_rad=[.3]*14,velocity_rad_s=[0.]*14) for i in range(100)]
    for role in ROLES:
        ep['cameras'][role]=[dict(timestamp_ns=BASE+OFFSET+(i+1)*33_000_000,
            frame_index=i,stored_width=640,stored_height=480) for i in range(30)]
    c._finish(ep,True)
    m=json.loads((ep['directory']/'meta_info.json').read_text())
    assert m['complete'] and m['gripper_quality_passed'] and m['action_samples']==100
    assert m['action_sampling']=='every_publication'
    for side in ('left','right'):
        assert m['gripper_statistics'][side]['samples']==100
    bundles=[json.loads(s) for s in (ep['directory']/'record/bundles.jsonl').read_text().splitlines()]
    keys=('state__joint__position','action__joint__position',
          'state__left_effector__position','state__right_effector__position',
          'action__left_effector__position','action__right_effector__position')
    for bundle in bundles:
        values=[v for k in keys for v in bundle['tensors'][k]]
        assert len(values)==32 and all(type(v) in (int,float) and math.isfinite(v) for v in values)
        assert bundle['tensors']['action__'+stationary_side+'_effector__position']==[-1.5]
        assert bundle['gripper_target_sources'][stationary_side]['source']=='initial_feedback'


@pytest.mark.parametrize('hz,quality',[(1,False),(10,False),(100,True)])
def test_real_sensor_rates_are_statistics_not_acceptance_gates(tmp_path,monkeypatch,hz,quality):
    c=recorder(tmp_path);c.options={'task':'synthetic'};c.session='fake'
    ep=c._begin({});frame(c);c._frames(ep);ep['events']=[packet(),motors()]
    end=time.monotonic_ns();ep['start_ns']=end-1_000_000_000
    ep['joint_samples']=[dict(kind='joint_sample',pc_monotonic_ns=end-1_000_000_000+i*1_000_000_000//hz,
        source_timestamp_ns=1_000_000_000+i*1_000_000_000//hz,source_sequence=i,
        joint_names=list(ARM_NAMES),position_rad=[.3]*14,velocity_rad_s=[.1]*14) for i in range(hz)]
    monkeypatch.setattr(time,'monotonic_ns',lambda:end)
    c._finish(ep,True)
    meta=json.loads((ep['directory']/'meta_info.json').read_text())
    assert meta['complete'] and meta['joint_quality_passed'] is quality
    assert meta['actual_joint_hz']==hz and meta['requested_joint_hz']==100
    assert meta['actual_action_hz']==1 and meta['full_rate_dataset'] is False
    assert len((ep['directory']/'record/joints.jsonl').read_text().splitlines())==hz
    assert len((ep['directory']/'record/aligned_joints.jsonl').read_text().splitlines())==hz
    assert (ep['directory']/READY_MARKER).exists()


@pytest.mark.parametrize('source',['joints','camera','gripper'])
def test_sparse_hour_long_episode_uploads_only_real_received_data(tmp_path,source):
    import httpx
    from teleop_control.collection_uploader import CollectionUploader, UPLOADED_MARKER
    c=recorder(tmp_path);c.options={};c.session='sparse'
    ep=c._begin({});ep['start_ns']-=3_600_000_000_000;ep['start_unix_ns']-=3_600_000_000_000
    if source=='joints':ep['joint_samples']=joints(1)
    elif source=='camera':
        c.manager.cameras.feeds={'left':c.manager.cameras.feeds['left']};frame(c);c._frames(ep)
    else:ep['events']=[motors()]
    c._finish(ep,True)
    m=json.loads((ep['directory']/'meta_info.json').read_text())
    assert m['complete'] and not m['joint_quality_passed'] and not m['actions_recorded']
    bundles=[json.loads(s) for s in (ep['directory']/'record/bundles.jsonl').read_text().splitlines()]
    assert len(bundles)==1
    assert all(v is None for k,v in bundles[0]['camera_frame_index'].items() if k!='left_color' or source!='camera')
    if source!='joints':assert bundles[0]['tensors']['state__joint__position']==[None]*14
    seen=[]
    def handle(request):
        seen.append(request)
        if request.url.host=='storage.test':return httpx.Response(200)
        if request.url.path=='/uploads/presign':
            name=json.loads(request.content)['filenames'][0]
            return httpx.Response(200,json={'urls':[{'filename':name,'put_url':'https://storage.test/'+name,'key':name}]})
        if request.url.path=='/episodes':return httpx.Response(201,json={})
        raise AssertionError(str(request.url))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        CollectionUploader(c.output,'https://platform.test',client=client)._upload_episode(
            ep['directory'],json.loads((ep['directory']/READY_MARKER).read_text()))
    assert (ep['directory']/UPLOADED_MARKER).exists()
    payload=json.loads(next(r.content for r in seen if r.url.path=='/episodes'))
    assert payload['extra']['acceptance_policy']=='received_data_without_rate_or_duration_gate'


@pytest.mark.parametrize('damage',['nan','names','clock_reversal','empty'])
def test_serious_corruption_or_empty_episode_never_enters_upload_queue(tmp_path,damage):
    c=recorder(tmp_path);c.options={};c.session='invalid'
    ep=c._begin({});samples=joints(2)
    if damage=='nan':samples[0]['position_rad'][0]=float('nan')
    if damage=='names':samples[0]['joint_names']=['wrong']*14
    if damage=='clock_reversal':samples[1]['source_timestamp_ns']=samples[0]['source_timestamp_ns']-1
    ep['joint_samples']=[] if damage=='empty' else samples
    c._finish(ep,True)
    assert c.phase=='incomplete' and c.error
    assert not (ep['directory']/READY_MARKER).exists()


def test_camera_reconnect_and_joint_gaps_keep_valid_samples(tmp_path):
    c=recorder(tmp_path);c.options={};c.session='gaps'
    ep=c._begin({});frame(c);c._frames(ep)
    c.manager.cameras.feeds={};c._frames(ep)
    c.manager.cameras.feeds={'head':Feed()};frame(c);c._frames(ep)
    samples=joints(3);samples[2]['source_sequence']=50
    samples[2]['source_timestamp_ns']+=60_000_000_000
    ep['joint_samples']=samples;c._finish(ep,True)
    m=json.loads((ep['directory']/'meta_info.json').read_text())
    assert m['complete'] and m['joint_samples']==3 and m['cameras']['head_color']['frames']==2
    assert m['quality_warnings']
