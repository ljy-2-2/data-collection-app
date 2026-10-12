"""Guarded standalone service restart; all SSH/USB/service calls are mocked."""
import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

spec=importlib.util.spec_from_file_location('restart_camera',Path(__file__).resolve().parents[1]/'scripts/restart_tianji_camera.py')
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)


def test_current_diagnosed_fault_is_accepted_but_other_failures_are_not():
    status=dict(version='2.3.2',idle=True,fault=mod.FAULT,last_stream=dict(native_exit=1,native_cleanup_timed_out=False,native_failure_reason='Camera open/start or negotiation failed',native_diagnostics_tail=['Camera 1 UVC -99: Unknown error']))
    mod.check_fault(status)
    for change in (dict(idle=False),dict(version='2.4'),dict(fault='Audit changed: modules'),dict(last_stream=dict(native_exit=-9))):
        with pytest.raises(RuntimeError):mod.check_fault(dict(status,**change))


@pytest.mark.parametrize('blocked',['running','moving','armed','busy','recording','preview'])
def test_active_tasks_refuse_restart(blocked):
    app=dict(status=dict(state='idle',collection={}),camera_thread_alive=False)
    mod.check_idle(app)
    if blocked in ('running','moving'):app['status']['state']=blocked
    elif blocked=='preview':app['camera_thread_alive']=True
    else:app['status']['collection'][blocked]=True
    with pytest.raises(RuntimeError):mod.check_idle(app)


@pytest.fixture
def remote(monkeypatch):
    tree=ast.parse(mod.REMOTE)
    ns={};exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) or (isinstance(n,ast.Import) and n.names[0].name!='streaming')],type_ignores=[]),'remote','exec'),ns)
    values={}
    for port,vid,pid,addr in [('1-1','0bda','5883','37'),('1-2','32e4','9230','40'),('1-4','32e4','9230','39')]:
        root='/sys/bus/usb/devices/'+port
        for k,v in dict(idVendor=vid,idProduct=pid,authorized='1',busnum='1',devnum=addr).items():values[root+'/'+k]=v.encode()
        values[root+':1.0/bAlternateSetting']=b'0'
    ns['read']=lambda path:values[path]
    ns['no_owner']=Mock()
    monkeypatch.setattr(ns['glob'],'glob',lambda pattern:[pattern[:-1]+'1.0'])
    monkeypatch.setattr(ns['os'].path,'exists',lambda path:False)
    return ns,values


def test_only_expected_idle_cameras_can_restart(remote):
    ns,values=remote
    assert ns['idle_devices']()=={'1-1':37,'1-2':40,'1-4':39}
    assert ns['no_owner'].call_count==4
    values['/sys/bus/usb/devices/1-2:1.0/bAlternateSetting']=b'6'
    with pytest.raises(RuntimeError,match='active'):ns['idle_devices']()


@pytest.mark.parametrize('field,value',[('idProduct',b'0000'),('authorized',b'0'),('busnum',b'2'),('devnum',b'1')])
def test_wrong_usb_identity_or_address_is_refused(remote,field,value):
    ns,values=remote;values['/sys/bus/usb/devices/1-2/'+field]=value
    with pytest.raises(RuntimeError):ns['idle_devices']()


@pytest.mark.parametrize('camera_ids,end_ok,native_exit,accepted',[
    (['usb-1','usb-2','usb-4'],True,0,True),
    (['usb-1','usb-2'],True,0,False),
    (['usb-1','usb-2','usb-4'],False,1,False),
])
def test_verification_requires_three_decodable_streams_and_clean_end(camera_ids,end_ok,native_exit,accepted):
    decoded=Mock()
    events=[SimpleNamespace(metadata=dict(type='image',camera_id=c),frame=lambda:SimpleNamespace(to_pil=lambda:SimpleNamespace(load=decoded))) for c in camera_ids]
    events.append(SimpleNamespace(metadata=dict(type='end',ok=end_ok,native_exit=native_exit)))
    client=SimpleNamespace(stream=lambda **kw:iter(events))
    if accepted:assert mod.verify_stream(client)['end_ok']
    else:
        with pytest.raises(RuntimeError):mod.verify_stream(client)
    assert decoded.call_count==len(camera_ids)


@pytest.mark.parametrize('failure',[None,'kernel_changed','unstable_kernel','source_changed','busy_device','controller_changed'])
def test_restart_transaction_only_after_all_fresh_checks(tmp_path,monkeypatch,failure):
    import base64,hashlib,json,sys
    root=tmp_path/'camera';root.mkdir()
    usb=tmp_path/'usb';usb.mkdir()
    for port,vid,pid,addr in [('1-1','0bda','5883','37'),('1-2','32e4','9230','40'),('1-4','32e4','9230','39')]:
        device=usb/port;device.mkdir();interface=usb/(port+':1.0');interface.mkdir()
        for k,v in dict(idVendor=vid,idProduct=pid,authorized='1',busnum='1',devnum=addr).items():(device/k).write_text(v)
        (interface/'bAlternateSetting').write_text('0')
    init=tmp_path/'service.conf';init.write_bytes(b'/mnt/fusion-camera/server.py')
    sources={}
    for name in mod.SOURCES:
        (root/name).write_text('test source');sources[name]=hashlib.sha256(b'test source').hexdigest()
    (root/'stream-manifest.json').write_text('{"sha256": {}}')
    audit=dict(robot_pids=['10','11'],modules='same',mounts='same',config_hashes={})
    kernel=b'usb 1-2: USB disconnect, device number 38\n'
    payload=dict(sources=sources,audit=audit,last_stream={},reviewed_kernel_sha256=hashlib.sha256(kernel).hexdigest())
    stream=SimpleNamespace(audit=lambda:dict(audit),unchanged_for_recovery=lambda *a:failure!='controller_changed')
    monkeypatch.setitem(sys.modules,'streaming',stream)
    monkeypatch.setattr(mod.os,'geteuid',lambda:0)
    monkeypatch.setattr(mod.time,'sleep',lambda seconds:None)
    calls=[]
    monkeypatch.setattr(mod.subprocess,'call',lambda *a,**kw:0 if failure=='busy_device' else 1)
    monkeypatch.setattr(mod.subprocess,'check_call',lambda args:calls.append(args))
    reads=[]
    def output(args):
        if args==['dmesg']:
            reads.append(True)
            return kernel+b'changed' if failure=='kernel_changed' or (failure=='unstable_kernel' and len(reads)>1) else kernel
        return b'fusion-camera start/running, process 123\n'
    monkeypatch.setattr(mod.subprocess,'check_output',output)
    if failure=='source_changed':(root/'server.py').write_text('unknown')
    code=mod.REMOTE.replace("root='/mnt/fusion-camera'",'root='+repr(str(root)))
    code=code.replace("read('/etc/init/fusion-camera.conf')",'read('+repr(str(init))+')')
    code=code.replace("'/sys/bus/usb/devices/'",repr(str(usb)+'/'))
    ns={'PAYLOAD':base64.b64encode(json.dumps(payload).encode())}
    if failure:
        with pytest.raises(RuntimeError):exec(code,ns)
        assert calls==[]
    else:
        exec(code,ns)
        assert calls==[['/sbin/stop','fusion-camera'],['/sbin/start','fusion-camera']]
        backups=list(root.glob('usb-disconnect-*'))
        assert len(backups)==1
        assert (backups[0]/'dmesg.before.txt').read_bytes()==kernel
