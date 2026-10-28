"""Real bundled preparation state machine; in-memory robot only, no sockets."""
import copy
import json
from pathlib import Path
import socket
import tempfile
from teleop_control.kuavo_worker import client_module
client_module()
from teleop_control.kuavo_preparation import prepare_controller
from teleop.robots.kuavo5_direct_channel import PreparationOutcomeUnknown
from teleop.robots.kuavo5_wizard_receipts import ReceiptStore
from teleop.robots.kuavo5_controller_admin import PREPARATION_ACTIONS, PREPARATION_PROTOCOL

def forbidden(*args, **kwargs):raise AssertionError('offline test attempted network I/O')
socket.socket.connect=forbidden
socket.create_connection=forbidden


class Robot:
    def __init__(self, phase='not_started'):
        self.phase=phase;self.calls=[];self.jobs=[];self.replies={};self.lose=None
        self.identity=dict(service_instance_id='a'*32, installation_sha256='b'*64, bundle_sha256='c'*64)
        self.controller_id={'pid':123,'start_ticks':456,'boot_id':'fake-boot'}
        self.contract='d'*64;self.busy=False;self.stuck=False;self.change_after=None

    def get_status(self):
        return dict(management_ready=True,service=dict(**self.identity,backend='robot',phase='idle',
            owner_present=self.busy,blocked_reason=None,robot_preparation_busy=False,
            robot_preparation_protocol=PREPARATION_PROTOCOL,robot_preparation_actions=list(PREPARATION_ACTIONS),
            session_contract={'protocol':'kuavo_robot_session_v3','contract_sha256':self.contract}))

    def state(self):
        identity=None if self.phase=='not_started' else self.controller_id
        return dict(phase=self.phase,manager={'phase':'not_started' if identity is None else 'process_running'},
            controller={'phase':self.phase,'controller_identity':identity,'parameters':{
                '/robot_version':63,'/hardware/is_ready':1 if self.phase=='ready' else 0,'/use_external_mpc':True}},
            context={'controller':identity})

    def preparation(self, action, **kw):
        assert action in ('status','start','initialize'), 'no gripper operations or teleoperation'
        assert kw['expected_service_instance_id']==self.identity['service_instance_id']
        assert kw['expected_installation_sha256']==self.identity['installation_sha256']
        assert kw.get('expected_contract_sha256')==(self.contract if self.get_status()['service']['session_contract']['protocol']=='kuavo_robot_session_v3' else None)
        if action=='status':body=self.state()
        else:
            assert kw['confirm_motion'] is True
            assert kw['motor_ids_confirmed'] is (action=='start')
            self.calls.append(action)
            if action=='start':
                assert self.phase=='not_started';self.phase='starting' if self.stuck else 'awaiting_confirmation'
            else:
                assert self.phase=='awaiting_confirmation';self.phase='ready'
            body={'accepted':True}
        rid=kw.get('request_id','0'*32)
        reply=dict(event='preparation_result',action=action,request_id=rid,error=None,
            service_instance_id=self.identity['service_instance_id'],installation_sha256=self.identity['installation_sha256'],
            result={'ok':True,'outcome_unknown':False,'helper_exit_code':0,'error':None,'result':body})
        self.replies[rid]=copy.deepcopy(reply)
        if action==self.change_after:self.contract='e'*64
        if action==self.lose:
            self.lose=None
            raise PreparationOutcomeUnknown(rid,'reply lost')
        return reply

    def preparation_job(self, rid, **kw):
        self.jobs.append(rid)
        return self.replies[rid]


def run(robot, directory, **changes):
    clock=[0.];events=[]
    options=dict(config=str(directory/'connection.json'),prepare_controller=True,confirm_motion=True,motor_ids_confirmed=True)
    options.update(changes)
    state=prepare_controller(robot,options,events.append,clock=lambda:clock[0],
        sleep=lambda delay:clock.__setitem__(0,clock[0]+delay),wait_s=2.)
    assert state['phase']=='ready'
    assert events[-1]['preparation_progress']==('厂家主控、初始化及左右夹爪准备检查已通过'
        if options.get('prepare_grippers') else '厂家主控与初始化已就绪')
    assert any('preparation' in event for event in events)
    return events


with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp)
    for phase, expected in [('not_started',['start','initialize']),('awaiting_confirmation',['initialize']),('ready',[])]:
        directory=root/phase;directory.mkdir()
        robot=Robot(phase);run(robot,directory)
        assert robot.calls==expected
        run(robot,directory);assert robot.calls==expected, 'repeated check must skip completed stages'
        for receipt in (directory/'preparation-receipts').glob('request-*.json'):
            assert json.loads(receipt.read_text())['status']=='resolved'
            assert receipt.stat().st_mode & 0o777==0o600

    for action in ('start','initialize'):
        directory=root/('lost-'+action);directory.mkdir()
        robot=Robot();robot.lose=action
        try:run(robot,directory)
        except PreparationOutcomeUnknown:pass
        else:raise AssertionError('lost reply was treated as success')
        with ReceiptStore(directory/'preparation-receipts') as store:pending=store.pending()
        assert pending['action']==action
        run(robot,directory)
        assert robot.jobs==[pending['request_id']] and robot.calls==['start','initialize']

    for name, setup in [('busy',lambda r:setattr(r,'busy',True)),
                         ('changed',lambda r:setattr(r,'change_after','start')),
                         ('stuck',lambda r:setattr(r,'stuck',True))]:
        directory=root/name;directory.mkdir();robot=Robot();setup(robot)
        try:run(robot,directory)
        except (ValueError,TimeoutError):pass
        else:raise AssertionError(name+' must not complete')
        assert robot.calls==([] if name=='busy' else ['start'])

    directory=root/'cancelled';directory.mkdir();robot=Robot()
    options=dict(config=str(directory/'connection.json'),prepare_controller=True,confirm_motion=True,motor_ids_confirmed=True)
    try:
        prepare_controller(robot,options,lambda _:None,sleep=lambda _:None,
                           cancelled=lambda:bool(robot.calls))
    except RuntimeError as exc:assert '检查已结束' in str(exc)
    else:raise AssertionError('parent exit must prevent the next stage')
    assert robot.calls==['start'], 'initialization must not run after parent exit'

    directory=root/'changed-service';directory.mkdir();robot=Robot();robot.lose='start'
    try:run(robot,directory)
    except PreparationOutcomeUnknown:pass
    robot.identity['service_instance_id']='f'*32
    try:run(robot,directory)
    except ValueError as exc:assert '旧服务' in str(exc)
    else:raise AssertionError('pending request must remain pinned to its original service')
    assert robot.calls==['start'] and robot.jobs==[]

    directory=root/'permission';directory.mkdir();robot=Robot()
    try:run(robot,directory,confirm_motion=False)
    except ValueError:pass
    else:raise AssertionError('missing authorization accepted')
    assert robot.calls==[] and not (directory/'preparation-receipts').exists()

    class GripperRobot(Robot):
        def __init__(self):
            super().__init__('ready')
            self.gripper_ready=False;self.right_error=None;self.bad_readback=False

        def get_status(self):
            status=super().get_status()
            status['service']['profile']={'gripper_config_sha256':'f'*64}
            status['service']['session_contract']['protocol']='kuavo_robot_session_v1'
            return status

        def preparation(self, action, **kw):
            if not action.startswith('grippers_'):return super().preparation(action,**kw)
            assert action in ('grippers_inspect','grippers_prepare')
            assert kw['expected_service_instance_id']==self.identity['service_instance_id']
            assert kw['expected_installation_sha256']==self.identity['installation_sha256']
            assert kw.get('expected_contract_sha256')==(self.contract if self.get_status()['service']['session_contract']['protocol']=='kuavo_robot_session_v3' else None)
            assert kw['accept_unverified_gripper_bus'] is True
            assert kw['confirm_motion'] is (action=='grippers_prepare')
            self.calls.append(action)
            if action=='grippers_prepare':self.gripper_ready=not self.bad_readback
            record={'ready':self.gripper_ready,'status':'ready' if self.gripper_ready else 'needs_preparation'}
            sides={side:copy.deepcopy(record) for side in ('left','right')}
            if self.right_error:sides['right'].update(status='failed',error=self.right_error,stage='preflight_identity')
            body={'result':{'ready':self.gripper_ready,'clients_closed':True,'cleanup_errors':[],
                            'error':None,'sides':sides}}
            rid=kw['request_id']
            reply=dict(event='preparation_result',action=action,request_id=rid,error=None,
                service_instance_id=self.identity['service_instance_id'],installation_sha256=self.identity['installation_sha256'],
                result={'ok':True,'outcome_unknown':False,'helper_exit_code':0,'error':None,'result':body})
            self.replies[rid]=copy.deepcopy(reply)
            if action==self.change_after:self.contract='e'*64
            if action==self.lose:
                self.lose=None
                raise PreparationOutcomeUnknown(rid,'reply lost')
            return reply

    gripper_options=dict(prepare_grippers=True,accept_unverified_gripper_bus=True,expected_gripper_sha256='f'*64)
    directory=root/'grippers';directory.mkdir();robot=GripperRobot()
    events=run(robot,directory,**gripper_options)
    assert robot.calls==['grippers_inspect','grippers_prepare']
    assert events[-1]['gripper_preparation_ready'] is True
    run(robot,directory,**gripper_options)
    assert robot.calls==['grippers_inspect','grippers_prepare','grippers_inspect']

    for name in ('right-failure','bad-readback','lost-prepare','lost-prepare-reset','gripper-contract-change','gripper-hash-mismatch'):
        directory=root/name;directory.mkdir();robot=GripperRobot();opts=gripper_options.copy()
        if name=='right-failure':robot.right_error='timeout'
        if name=='bad-readback':robot.bad_readback=True
        if name.startswith('lost-prepare'):robot.lose='grippers_prepare'
        if name=='gripper-contract-change':robot.change_after='grippers_inspect'
        if name=='gripper-hash-mismatch':opts['expected_gripper_sha256']='0'*64
        try:run(robot,directory,**opts)
        except (ValueError,PreparationOutcomeUnknown):pass
        else:raise AssertionError(name+' must fail closed')
        if name in ('right-failure','gripper-contract-change','gripper-hash-mismatch'):
            assert 'grippers_prepare' not in robot.calls
        if name.startswith('lost-prepare'):
            if name=='lost-prepare-reset':robot.gripper_ready=False
            try:run(robot,directory,**opts)
            except ValueError:assert name=='lost-prepare-reset'
            else:assert name=='lost-prepare'
            assert len(robot.jobs)==1 and robot.calls.count('grippers_prepare')==1

    directory=root/'gripper-cancel';directory.mkdir();robot=GripperRobot()
    options=dict(config=str(directory/'connection.json'),prepare_controller=True,confirm_motion=True,
                 motor_ids_confirmed=True,**gripper_options)
    try:prepare_controller(robot,options,lambda _:None,cancelled=lambda:'grippers_inspect' in robot.calls)
    except RuntimeError:pass
    else:raise AssertionError('cancelled check must not write gripper parameters')
    assert robot.calls==['grippers_inspect']

    # A current service derives its gripper configuration from validated online
    # settings. An old local calibration digest must not block that workflow.
    from teleop.robots.kuavo5_service_contract import prepare_service_contract, bind_service_contract
    settings=json.loads(Path('tests/fixtures/kuavo_settings/snapshot.json').read_text())
    class OnlineRobot(GripperRobot):
        def __init__(self):
            super().__init__()
            self.identity.update(installation_sha256=settings['installation_sha256'],bundle_sha256=settings['bundle_sha256'])
            bundle=dict(target_recovery='bounded_candidate',speed_profile='responsive',
                arm_posture='elbow_preference',gripper_mode='damiao',gripper_config_sha256='f'*64,
                return_home_config={},return_home_config_sha256='e'*64)
            self.descriptor=bind_service_contract(prepare_service_contract(bundle,settings),**self.identity)
            self.contract=self.descriptor['contract_sha256']
            self.tamper=False

        def get_status(self):
            status=super().get_status()
            status['service'].update(event='service_status',session_duration_policy='zero_is_unbounded_v1',
                session_contract=copy.deepcopy(self.descriptor),profile=copy.deepcopy(self.descriptor['profile']))
            if self.tamper:status['service']['session_contract']['session_settings']['values']['left_arm_scale']=.3
            if self.contract!=self.descriptor['contract_sha256']:
                status['service']['session_contract']['contract_sha256']=self.contract
            return status

    directory=root/'online-stale-local';directory.mkdir();robot=OnlineRobot()
    run(robot,directory,**{**gripper_options,'expected_gripper_sha256':'0'*64})
    assert robot.calls==['grippers_inspect','grippers_prepare']
    for name in ('online-tampered','online-changed'):
        directory=root/name;directory.mkdir();robot=OnlineRobot()
        if name=='online-tampered':robot.tamper=True
        else:robot.change_after='grippers_inspect'
        try:run(robot,directory,**gripper_options)
        except ValueError:pass
        else:raise AssertionError('invalid or changed online contract must be rejected')
        assert 'grippers_prepare' not in robot.calls
print('App-only startup/initialize, ready skip, repeated clicks, lost reply recovery, identity/contract pins and timeout passed')
