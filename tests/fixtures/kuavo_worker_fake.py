import io,json,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from teleop_control import kuavo_worker as w
module=w.client_module()
assert module.ROOT==w.VENDOR
assert module.host_module().ROOT==w.VENDOR
from teleop.robots.kuavo5_pc_session import SessionControl
with tempfile.TemporaryDirectory() as tmp:
    output=Path(tmp)/'episode'
    events=[]
    def host(*args,**kw):
        adapter=kw['xr_factory']()
        assert adapter.mode=='dual' and adapter.scale==1.
        kw['session_control'].ready()
        kw['journal'].write(json.dumps({'packet':{'event':'status','reason':'fake'}})+'\n')
        kw['session_control'].set_report({'event':'host_closed','errors':[],'exit_code':0})
        assert kw['duration']==0 and kw['service_contract']['shared_bus_compatibility_verified'] is False
        return 0
    fake=SimpleNamespace(select_session=lambda *a:{'shared_bus_compatibility_verified':False},host_module=lambda:SimpleNamespace(run_host=host),channel_factory=Mock(side_effect=AssertionError('no network')))
    with patch('signal.signal'), patch('threading.Thread.start'), patch('teleop.robots.kuavo5_service_contract.host_bundle',lambda c:c):
        code=w.execute(fake,{},dict(network_namespace='current',accept_unverified_gripper_bus=True,output=str(output),task='test'),events.append)
    assert code==0
    meta=json.loads((output/'meta.json').read_text())
    assert meta['complete'] is True and meta['image_dataset'] is False
    assert events[-1]['session']['phase']=='finished'
    assert 'fake' in (output/'events.jsonl').read_text()
    fake.host_module=Mock(side_effect=AssertionError('should not start'))
    with patch('signal.signal'), patch('threading.Thread.start'):
        code=w.execute(fake,{},dict(network_namespace='current',accept_unverified_gripper_bus=False,output=str(Path(tmp)/'refused'),task='test'),events.append)
    assert code==1 and events[-1]['session']['phase']=='failed'
    assert not (Path(tmp)/'refused').exists()
    fake.host_module.assert_not_called()
print('isolated worker fake lifecycle passed; no network or SDK initialized')

# Exercise the production wrapper with original immutable XR types; no SDK init.
from dataclasses import replace
from teleop.contracts.xr import Pose, ControllerButtons, ControllerState, XRFrame
from teleop_control.kuavo_input import InputAdapter
pose=Pose((1.,2.,3.),(0.,0.,0.,1.))
hand=ControllerState(pose,(.2,-.3),1.,.8,ControllerButtons(primary=True,secondary=True))
frame=XRFrame(42,100,200,False,pose,hand,hand)
source=SimpleNamespace(read_frame=lambda:frame,start=Mock(),close=Mock())
assert InputAdapter(source).read_frame() is frame
for mode,side in [('left','right_controller'),('right','left_controller')]:
    adapter=InputAdapter(source,mode,.5)
    sample=adapter.read_frame()
    disabled=getattr(sample,side)
    assert disabled.grip==disabled.trigger==0.
    assert getattr(sample,('left_controller' if mode=='left' else 'right_controller')).grip==1.
    assert not sample.left_controller.buttons.secondary  # Bilateral Y is suppressed.
    assert sample.right_controller.buttons.secondary     # B cancellation remains.
    assert sample.left_controller.buttons.primary and sample.right_controller.buttons.primary
    assert sample.left_controller.pose.position_m==(.5,1.,1.5)
    assert sample.left_controller.pose.orientation_xyzw==pose.orientation_xyzw
    assert (sample.sequence_id,sample.device_timestamp_ns,sample.received_monotonic_ns)==(42,100,200)
    adapter.start();adapter.close()
assert source.start.call_count==source.close.call_count==2
assert frame.left_controller.grip==frame.right_controller.grip==1., 'source never mutated'
source.read_frame=lambda:None
assert InputAdapter(source,'left',.5).read_frame() is None
source.read_frame=lambda:replace(frame,stale=True)
assert InputAdapter(source,'right',.5).read_frame().stale
print('original XR types: side isolation, identity defaults, buttons, scale and timestamps passed')

from teleop.retargeting.dual_arm import DualArmRetargeter
from teleop.contracts.intent import CartesianPose
anchors={s:CartesianPose('base_link',(0.,0.,0.),(0.,0.,0.,1.)) for s in ('left','right')}
for mode in ('dual','left','right'):
    expected={'left','right'} if mode=='dual' else {mode}
    current=[frame]
    source.read_frame=lambda:current[0]
    adapter=InputAdapter(source,mode,.5)
    retarget=DualArmRetargeter()
    assert set(retarget.update(adapter.read_frame(),anchors).intent.targets)==expected
    moved=replace(hand,pose=replace(pose,position_m=(1.2,2.,3.)))
    current[0]=replace(frame,sequence_id=43,received_monotonic_ns=201,left_controller=moved,right_controller=moved)
    result=retarget.update(adapter.read_frame(),anchors)
    assert set(result.intent.targets)==expected
    for value in result.intent.targets.values():
        assert abs(sum(v*v for v in value.position_m)**.5-.1)<1e-9
print('original retargeter: 20 cm hand motion -> 10 cm selected-arm intent; inactive arm has no intent')
