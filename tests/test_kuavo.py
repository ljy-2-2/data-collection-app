"""KUAVO integration tests: fake subprocesses only; never connect devices."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from teleop_control.kuavo import KuavoManager, Options, kuavo_router

@pytest.fixture
def manager(tmp_path, monkeypatch):
    m=KuavoManager(SimpleNamespace(hardware_enabled=True),root=tmp_path)
    cfg=tmp_path/'teleop_control/runtime/kuavo/connection.json'
    cfg.parent.mkdir(parents=True);cfg.write_text('{}')
    monkeypatch.setattr('teleop_control.kuavo.add_xr_runtime_env',lambda env,*_:env)
    return m


def test_construction_and_status_are_inert(manager,monkeypatch):
    monkeypatch.setattr('subprocess.Popen',Mock(side_effect=AssertionError('no device IO')))
    monkeypatch.setattr('subprocess.run',Mock(side_effect=AssertionError('no device IO')))
    assert manager.snapshot()['state']=='idle'
    assert not manager.active()
    manager.close()


def test_mock_mode_and_unconfirmed_start_never_launch(manager,monkeypatch):
    launch=Mock();monkeypatch.setattr('subprocess.Popen',launch)
    with pytest.raises(ValueError,match='明确'):manager.start({})
    manager.controller.hardware_enabled=False
    with pytest.raises(ValueError,match='模拟'):manager.start({'confirm_motion':True})
    launch.assert_not_called()


def test_existing_robot_owner_blocks_kuavo(manager,monkeypatch):
    manager.other_idle=Mock(side_effect=RuntimeError('other robot active'))
    launch=Mock();monkeypatch.setattr('subprocess.Popen',launch)
    with pytest.raises(RuntimeError,match='other robot'):manager.start({'confirm_motion':True})
    with pytest.raises(RuntimeError,match='other robot'):manager.inspect({})
    assert not manager.active()
    launch.assert_not_called()


def test_check_only_queries_metadata_and_uses_relocated_worker(manager,monkeypatch):
    run=Mock(return_value=SimpleNamespace(returncode=0,stdout='KUAVO:'+json.dumps({
        'service':{'management_ready':True},'preparation':{'phase':'ready'},'pico_sdk_available':True})+'\n',stderr=''))
    monkeypatch.setattr('subprocess.run',run)
    manager.inspect(Options().model_dump())
    args,kw=run.call_args
    assert args[0][-1]=='check'
    assert args[0][-2]=='teleop_control.kuavo_worker'
    assert json.loads(kw['input'])['network_namespace']=='current'
    assert str(manager.root) in kw['env']['PYTHONPATH']
    assert not manager.active()
    assert manager.snapshot()['service']['management_ready']


def test_check_failure_is_visible_and_releases_operation(manager,monkeypatch):
    monkeypatch.setattr('subprocess.run',Mock(return_value=SimpleNamespace(returncode=1,stdout='KUAVO:{"error":"TLS mismatch"}\n',stderr='')))
    with pytest.raises(RuntimeError,match='TLS mismatch'):manager.inspect({})
    assert manager.snapshot()['state']=='error'
    assert not manager.active() and manager.operation.acquire(blocking=False)
    manager.operation.release()


def test_start_isolated_and_stop_only_owns_current_session(manager,monkeypatch):
    process=SimpleNamespace(stdin=io.StringIO(),stdout=io.StringIO(),stderr=io.StringIO(),poll=lambda:None)
    launch=Mock(return_value=process);monkeypatch.setattr('subprocess.Popen',launch)
    monkeypatch.setattr('threading.Thread.start',lambda _:None)
    options=Options(task='测试任务',confirm_motion=True,accept_unverified_gripper_bus=True).model_dump()
    value=manager.start(options)
    assert value['state']=='starting' and value['active']
    payload=json.loads(process.stdin.getvalue())
    assert payload['task']=='测试任务' and payload['accept_unverified_gripper_bus']
    assert str(manager.root) in payload['output']
    assert launch.call_args.args[0][-1]=='execute'
    with pytest.raises(RuntimeError):manager.start(options)
    with pytest.raises(RuntimeError):manager.inspect({})
    with pytest.raises(ValueError,match='会话编号'):manager.stop('other-session')
    assert 'stop\n' not in process.stdin.getvalue()
    manager.stop(value['session_id'])
    assert process.stdin.getvalue().endswith('stop\n')
    assert manager.snapshot()['state']=='stopping'


def test_closed_report_errors_are_not_reported_as_success(manager):
    manager._merge({'session':{'phase':'failed','report':{'errors':['cleanup timeout']}}})
    assert manager.snapshot()['state']=='error'
    assert manager.snapshot()['error']=='cleanup timeout'


def test_api_unknown_fields_and_actions_rejected():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):Options(preparation_action='reset-all')
    with pytest.raises(ValidationError):Options(robot_ip='anything')
    with pytest.raises(ValidationError):Options(network_namespace='auto-discover')
    assert {r.path for r in kuavo_router(Mock()).routes}=={'/kuavo/status','/kuavo/check','/kuavo/start','/kuavo/stop','/kuavo/prepare','/kuavo/grippers','/kuavo/connection'}


def test_vendor_source_integrity_and_no_external_source_links():
    import hashlib
    root=Path(__file__).resolve().parents[1]/'teleop_control/vendor/kuavo_runtime'
    manifest=json.loads((root/'source-manifest.json').read_text())
    for name,digest in manifest['sha256'].items():
        path=root/name
        assert not path.is_symlink()
        assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
    assert not list(root.rglob('*.key'))
    assert not list(root.rglob('token'))


def test_isolated_worker_preserves_original_host_and_journal():
    import os,subprocess,sys
    root=Path(__file__).resolve().parents[1]
    env={**os.environ,'PYTHONPATH':os.pathsep.join((str(root/'teleop_control/src'),os.environ.get('PYTHONPATH','')))}
    result=subprocess.run([sys.executable,str(root/'tests/fixtures/kuavo_worker_fake.py')],
                          env=env,text=True,capture_output=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr


def test_launch_failure_does_not_leave_starting_state(manager,monkeypatch):
    monkeypatch.setattr('subprocess.Popen',Mock(side_effect=FileNotFoundError('missing python')))
    with pytest.raises(FileNotFoundError):manager.start({'confirm_motion':True})
    assert manager.snapshot()['state']=='error' and not manager.active()


def test_backend_ownership_middleware_blocks_other_robots(monkeypatch):
    import asyncio
    from starlette.requests import Request
    from starlette.responses import Response
    from teleop_control.app import create_app
    from teleop_control.main import MockController
    app=create_app(MockController(),cors_origins=[])
    manager=app.state.kuavo_manager
    manager.pending_operation=True
    middleware=next(m.kwargs['dispatch'] for m in app.user_middleware if 'dispatch' in m.kwargs)
    calls=[]
    async def next_(request):
        calls.append(request.url.path)
        return Response(status_code=204)
    async def run():
        for path in ['/tianji/check','/tianji/start','/tianji/move','/sessions','/devices/check','/umi/check','/umi/sessions']:
            request=Request({'type':'http','method':'POST','path':path,'headers':[]})
            assert (await middleware(request,next_)).status_code==409
        for method,path in [('GET','/kuavo/status'),('POST','/kuavo/stop'),('POST','/tianji/stop')]:
            request=Request({'type':'http','method':method,'path':path,'headers':[]})
            assert (await middleware(request,next_)).status_code==204
    asyncio.run(run())
    assert len(calls)==3


def test_previous_process_cannot_overwrite_new_session(manager):
    old=SimpleNamespace(stdout=io.StringIO('KUAVO:{"session":{"phase":"failed","error":"old error"}}\n'),
                        stdin=io.StringIO(),wait=lambda:1)
    current=SimpleNamespace(poll=lambda:None)
    manager.process=current
    manager.last_status=__import__('time').monotonic()
    manager.value['state']='running'
    manager._read(old)
    assert manager.snapshot()['state']=='running'
    assert manager.snapshot()['error']==''
