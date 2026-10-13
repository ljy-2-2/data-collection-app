"""Mocked USB/service calls only; never reset or open a real device."""
import ast
import fcntl
import importlib.util
import os
from pathlib import Path
import signal
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location('camera_diagnose', Path(__file__).resolve().parents[1]/'scripts/diagnose_tianji_camera.py')
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


@pytest.fixture
def reset_env(monkeypatch):
    tree = ast.parse(diag.REMOTE)
    definitions = [node for node in tree.body if isinstance(node,(ast.Import,ast.FunctionDef))]
    ns = {}
    exec(compile(ast.Module(body=definitions,type_ignores=[]),'remote','exec'),ns)
    root = '/sys/bus/usb/devices/1-1'
    values = {root+'/idVendor':'0bda',root+'/idProduct':'5883',root+'/authorized':'1',
              root+'/busnum':'1',root+'/devnum':'2',root+':1.0/bAlternateSetting':'0'}
    ns['read'] = lambda path: values[path]
    monkeypatch.setattr(ns['glob'],'glob',lambda path:[root+':1.0'])
    monkeypatch.setattr(os.path,'exists',lambda path:False)
    monkeypatch.setattr(os,'geteuid',lambda:0)
    opened,closed,ioctl = Mock(return_value=77),Mock(),Mock()
    monkeypatch.setattr(os,'open',opened)
    monkeypatch.setattr(os,'close',closed)
    monkeypatch.setattr(os,'fstat',lambda fd:SimpleNamespace(st_mode=stat.S_IFCHR,st_rdev=os.makedev(189,1)))
    monkeypatch.setattr(fcntl,'ioctl',ioctl)
    monkeypatch.setattr(signal,'signal',Mock())
    monkeypatch.setattr(signal,'alarm',Mock())
    monkeypatch.setattr(ns['time'],'sleep',Mock())

    def command(args):
        if args[0]=='/bin/fuser': return dict(code=1,text='')
        if args[0]=='/usr/bin/pgrep':
            return dict(code=0,text='10\n11\n12\n13\n') if 'FXProducer' in args[-1] else dict(code=1,text='')
        return dict(code=0,text='fusion-camera start/running' if args[0]=='/sbin/status' else '')

    ns['command'] = Mock(side_effect=command)
    return SimpleNamespace(ns=ns,values=values,root=root,opened=opened,closed=closed,ioctl=ioctl)


def test_reset_targets_only_verified_main_camera(reset_env):
    e=reset_env
    result=e.ns['reset_main_camera']()
    assert result['ok'] and result['reset_completed'] and result['robot_processes_unchanged']
    e.opened.assert_called_once_with('/dev/bus/usb/001/002',os.O_RDWR|os.O_NOFOLLOW)
    e.ioctl.assert_called_once_with(77,0x5514,0)
    e.closed.assert_called_once_with(77)
    service_calls=[c.args[0] for c in e.ns['command'].call_args_list if c.args[0][0].startswith('/sbin/')]
    assert service_calls==[['/sbin/stop','fusion-camera'],['/sbin/start','fusion-camera'],['/sbin/status','fusion-camera']]


@pytest.mark.parametrize('change', ['wrong_device','hub_address','active_interface','owned','driver'])
def test_unsafe_or_busy_target_refuses_before_any_reset(reset_env,monkeypatch,change):
    e=reset_env
    if change=='wrong_device':e.values[e.root+'/idProduct']='9230'
    if change=='hub_address':e.values[e.root+'/devnum']='1'
    if change=='active_interface':e.values[e.root+':1.0/bAlternateSetting']='4'
    if change=='owned':e.ns['command']=Mock(return_value=dict(code=0,text='capture owns camera'))
    if change=='driver':monkeypatch.setattr(os.path,'exists',lambda path:True)
    assert not e.ns['reset_main_camera']()['ok']
    e.ioctl.assert_not_called()
    assert not any(c.args[0][0]=='/sbin/stop' for c in e.ns['command'].call_args_list)


def test_failed_reset_restarts_only_camera_service_without_retry(reset_env):
    e=reset_env
    e.ioctl.side_effect=OSError('reset rejected')
    result=e.ns['reset_main_camera']()
    assert not result['ok'] and not result['reset_completed']
    assert result['error']=='reset rejected'
    e.ioctl.assert_called_once()
    assert any(c.args[0]==['/sbin/start','fusion-camera'] for c in e.ns['command'].call_args_list)


def test_reset_preflight_rejects_motion_and_unrelated_faults():
    status=dict(idle=True,version='2.3.2',last_stream=dict(native_exit=1),
                fault='Stream stopped: Native helper failed (exit 1): Camera did not produce first frame after bounded startup retry; stop')
    diag.check_reset_allowed(dict(state='idle'),status)
    for app in (dict(state='moving'),dict(state='running'),dict(state='idle',collection=dict(busy=True))):
        with pytest.raises(RuntimeError):diag.check_reset_allowed(app,status)
    with pytest.raises(RuntimeError):diag.check_reset_allowed(dict(state='idle'),dict(status,fault='Audit changed: modules'))


def test_reset_accepts_failed_snapshot_only_after_same_verified_first_frame_timeout():
    reason='Camera did not produce first frame after bounded startup retry; stop'
    status=dict(idle=True,version='2.3.2',fault='Worker failed (exit 1); manual inspection required',
                last_stream=dict(native_exit=1,native_cleanup_timed_out=False,
                                 error='Native helper failed (exit 1): '+reason,native_failure_reason=reason))
    diag.check_reset_allowed(dict(state='idle'),status)
    status['last_stream']['error']='Audit changed: dmesg_sha256'
    with pytest.raises(RuntimeError):diag.check_reset_allowed(dict(state='idle'),status)


@pytest.mark.parametrize('cameras,end_ok,expected', [(['usb-1','usb-2','usb-4'],True,True),(['usb-1'],True,False),(['usb-1','usb-2','usb-4'],False,False)])
def test_reset_verification_requires_all_three_frames_and_clean_end(cameras,end_ok,expected):
    events=[SimpleNamespace(metadata=dict(type='image',camera_id=c)) for c in cameras]
    events.append(SimpleNamespace(metadata=dict(type='end',ok=end_ok)))
    client=SimpleNamespace(status=lambda:dict(idle=True),stream=lambda **kw:iter(events))
    assert diag.verify_frames(client)['ok'] is expected
