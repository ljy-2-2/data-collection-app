"""Headset shutdown and launch cancellation; all ADB calls are simulated."""
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'teleop_control/vendor/tianji/teleop'))
from marvin_teleop import launcher


@pytest.fixture
def pico():
    p = launcher.PicoLauncher(dict(serial='', package='com.galaxis.vrteleop', input='sdk'))
    p.serial = p.verified_serial = 'PICO-TEST'
    p.app_started = True
    return p


@pytest.mark.parametrize('mode', ['sdk', 'tcp'])
def test_close_stops_only_selected_sender_and_removes_tunnel(pico, monkeypatch, mode):
    calls = []
    pico.cfg['input'] = mode
    pico.saved_reverse = 'tcp:old-service'
    monkeypatch.setattr(launcher.subprocess, 'run', lambda cmd, **kw:
        calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, '', ''))
    pico.close()
    pico.close()
    assert calls == [
        ['adb', '-s', 'PICO-TEST', 'shell', 'am', 'force-stop', 'com.galaxis.vrteleop'],
        ['adb', '-s', 'PICO-TEST', 'reverse', '--remove', 'tcp:63901']]
    assert pico.closed and not pico.app_started and pico.done.is_set()
    with pytest.raises(RuntimeError, match='closed'):
        pico.ensure()
    assert len(calls) == 2


def test_close_cancels_inflight_ensure_before_it_can_relaunch(pico, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:] == ['devices', '-l']:
            entered.set()
            assert release.wait(2)
            return subprocess.CompletedProcess(cmd, 0, 'List of devices attached\nPICO-TEST device\n', '')
        return subprocess.CompletedProcess(cmd, 0, '', '')
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    pico.start()
    assert entered.wait(2)
    finished = threading.Thread(target=pico.close)
    finished.start()
    assert pico.done.wait(2)
    release.set()
    finished.join(3)
    assert not finished.is_alive() and not pico.thread.is_alive()
    assert not any('monkey' in cmd or 'getprop' in cmd for cmd in calls)
    assert calls[-1][-3:] == ['reverse', '--remove', 'tcp:63901']


@pytest.mark.parametrize('connected', [True, False])
def test_cleanup_attempts_both_paths_and_reports_failure_if_still_online(pico, monkeypatch, connected):
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[-1] == 'devices':
            return subprocess.CompletedProcess(cmd, 0, 'PICO-TEST\tdevice' if connected else '', '')
        return subprocess.CompletedProcess(cmd, 1, '', 'USB command failed')
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    if connected:
        with pytest.raises(RuntimeError, match='PICO 断开失败'):pico.close()
        assert not pico.closed
    else:
        pico.close()
        assert pico.closed
    assert any('force-stop' in cmd for cmd in calls)
    assert any('--remove' in cmd for cmd in calls)


def test_close_accepts_already_removed_tunnel(pico, monkeypatch):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1 if '--remove' in cmd else 0, '', "listener 'tcp:63901' not found")
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    pico.close()
    assert pico.closed


@pytest.mark.parametrize('exit_method', ['page', 'close', 'signal'])
def test_worker_stop_keeps_connection_and_page_exit_preserves_app(monkeypatch, tmp_path, exit_method):
    import io
    import json
    import queue
    import signal
    from types import SimpleNamespace
    import numpy as np
    from teleop_control import tianji_worker as worker
    incoming = queue.Queue()
    headsets, inputs, robots, handlers = [], [], [], {}
    phase = ['check']
    def command(action, **payload):incoming.put(json.dumps({'action':action,'id':action,'payload':payload})+'\n')
    class Input:
        def __iter__(self):
            while True:
                line=incoming.get(timeout=5)
                if line is None:return
                yield line
    class Robot:
        def __init__(self, config):
            self.q={s:np.zeros(7) for s in ('left','right')}
            self.last_command=None;self.closed=False;self.stops=0;robots.append(self)
        def wait_ready(self):pass
        def read(self):return self.q
        def stop(self):self.stops+=1
        def close(self):self.closed=True
    class Headset:
        def __init__(self,config):self.app_started=True;self.status='ready';self.closed=False;headsets.append(self)
        def start(self):pass
        def close(self, *, quit_app=True):
            self.closed=True
            if quit_app:self.app_started=False
    class VR:
        def __init__(self,*a,**kw):self.closed=False;self.checks=0;inputs.append(self)
        def check(self):self.checks+=1
        def close(self):self.closed=True
        def snapshot(self):
            assert not self.closed
            return None,False,'',True
    class Publisher:
        def __init__(self,**kw):self.count=0
        def submit(self,row,*args):
            self.count+=1;assert self.count<100
            if phase[0]=='check' and headsets:
                command('stop');phase[0]='stop'
            elif phase[0]=='stop' and robots[0].stops:
                assert not inputs[0].closed and not headsets[0].closed
                assert not robots[0].closed and args[-1] is True
                command('check');phase[0]='recheck'
            elif phase[0]=='recheck' and inputs[0].checks==2:
                assert len(headsets)==len(inputs)==1, 'End teleop must retain the connection'
                phase[0]='complete'
                if exit_method=='signal':handlers[signal.SIGTERM](signal.SIGTERM,None)
                else:command('close',quit_pico=exit_method=='close')
        def close(self):pass
    monkeypatch.setattr(worker,'__file__',str(tmp_path/'src/teleop_control/tianji_worker.py'))
    monkeypatch.setattr(worker.sys,'stdin',Input())
    monkeypatch.setattr(worker.sys,'stdout',io.StringIO())
    monkeypatch.setattr(worker.signal,'signal',lambda sig,fn:handlers.__setitem__(sig,fn))
    monkeypatch.setattr(worker,'load_configuration',lambda:{'arm_geometry':'nominal'})
    monkeypatch.setattr(worker,'Models',lambda c:SimpleNamespace(validate=lambda:None,mapping_report={}))
    monkeypatch.setattr(worker,'validate_preview',lambda *a:None)
    monkeypatch.setattr(worker,'RecordingRobot',Robot)
    monkeypatch.setattr(worker,'Publisher',Publisher)
    monkeypatch.setattr(worker,'PicoLauncher',Headset)
    monkeypatch.setattr(worker,'LocalVRConnection',VR)
    command('check')
    try:worker.main()
    finally:incoming.put(None)
    assert phase[0]=='complete'
    assert all(p.closed for p in headsets+inputs+robots)

    assert headsets[0].app_started == (exit_method!='close')


def test_page_disconnect_keeps_app_and_later_shutdown_exits_it(pico, monkeypatch):
    calls=[]
    monkeypatch.setattr(launcher.subprocess,'run',lambda cmd,**kw:
        calls.append(cmd) or subprocess.CompletedProcess(cmd,0,'',''))
    pico.close(quit_app=False)
    assert pico.closed and pico.app_started and not pico.app_stopped
    assert len(calls)==1 and '--remove' in calls[0]
    pico.close(quit_app=True)
    assert any('force-stop' in cmd for cmd in calls[1:])
    assert pico.app_stopped and not pico.app_started


def test_reconnect_reuses_running_headset_app(pico,monkeypatch):
    calls=[]
    pico.app_started=False
    pico.cfg['port']=63901
    monkeypatch.setattr(pico,'devices',lambda:'PICO-TEST')
    monkeypatch.setattr(pico,'require_pico',lambda:None)
    def adb(*args,**kw):
        calls.append(args)
        if 'packages' in args:return 'package:com.galaxis.vrteleop'
        if 'pidof' in args:return '1234'
        return ''
    monkeypatch.setattr(pico,'adb',adb)
    pico.ensure()
    assert pico.app_started and not any('monkey' in cmd for cmd in calls)


def test_manager_page_departure_then_app_exit_stops_remembered_headset(monkeypatch):
    import io,json
    from unittest.mock import Mock
    from teleop_control.tianji import TianjiManager,tianji_router
    from teleop_control.main import MockController
    from teleop_control.galaxis import GalaxisHeadsetSetup
    manager=TianjiManager(MockController())
    process=Mock(stdout=io.StringIO('TIANJI:'+json.dumps({'status':{'state':'idle'},'headset':{'serial':'PICO-TEST','package':'com.galaxis.vrteleop'}})+'\n'))
    process.wait.return_value=0
    manager.process=process
    manager._read(process)
    process.poll.return_value=0
    stop=Mock(return_value=True)
    monkeypatch.setattr(GalaxisHeadsetSetup,'stop_app',stop)
    next(route.endpoint for route in tianji_router(manager).routes if route.path=='/tianji/close')()
    stop.assert_not_called()
    assert manager.headset_apps=={('PICO-TEST','com.galaxis.vrteleop')}
    manager.close()
    stop.assert_called_once_with('PICO-TEST')
    assert not manager.headset_apps
