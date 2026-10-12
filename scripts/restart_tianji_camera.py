#!/usr/bin/env python3
"""Explicit recovery after an inspected camera USB disconnect; no robot commands."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FAULT = 'Camera interface did not return to idle; Audit changed: dmesg_sha256'
# Known installed v2.3.2 sources, including the reviewed bounded audit policy.
SOURCES = {
    'streaming.py': '5b52704aa33a0e947247d6f4414338b5dcf05ca08f9bf0b4b9cfbca27fc815bc',
    'server.py': '9ce41154c1df0437b3d02b6df3068642f59b6a71987ca8e4b28c0db62b8e16d2',
    'camera_audit.py': '008d015123adffb383a9aeead428eb10233003073e639a022b61a5d46a1731d1',
}
REMOTE = r'''
import base64,glob,hashlib,json,os,subprocess,sys,tempfile,time
payload=json.loads(base64.b64decode(PAYLOAD))
root='/mnt/fusion-camera'
def read(path):
    with open(path,'rb') as f:return f.read()
def output(args):return subprocess.check_output(args)
def no_owner(args):
    with open(os.devnull,'wb') as null:
        if subprocess.call(args,stdout=null,stderr=null)!=1:
            raise RuntimeError('Active camera or failed ownership check')
def idle_devices():
    no_owner(['/usr/bin/pgrep','-x','capture|capture-three|capture-three-t'])
    result={}
    for port,identity in [('1-1',('0bda','5883')),('1-2',('32e4','9230')),('1-4',('32e4','9230'))]:
        path='/sys/bus/usb/devices/'+port
        if tuple(read(path+'/'+k).strip().decode() for k in ('idVendor','idProduct'))!=identity:
            raise RuntimeError('Unexpected or missing camera: '+port)
        if read(path+'/authorized').strip()!=b'1':raise RuntimeError('Unauthorized camera')
        interfaces=glob.glob(path+':*')
        if not interfaces:raise RuntimeError('Missing camera interfaces')
        for interface in interfaces:
            if os.path.exists(interface+'/driver') or int(read(interface+'/bAlternateSetting').strip(),16)!=0:
                raise RuntimeError('Camera interface is active: '+port)
        bus,address=int(read(path+'/busnum')),int(read(path+'/devnum'))
        if bus!=1 or not 2<=address<=127:raise RuntimeError('Unexpected camera USB address')
        no_owner(['/bin/fuser','/dev/bus/usb/%03d/%03d'%(bus,address)])
        result[port]=address
    return result
if os.geteuid()!=0:raise RuntimeError('Controller root login required')
if b'/mnt/fusion-camera/server.py' not in read('/etc/init/fusion-camera.conf'):
    raise RuntimeError('Unexpected service layout')
for name,digest in payload['sources'].items():
    if hashlib.sha256(read(root+'/'+name)).hexdigest()!=digest:
        raise RuntimeError('Camera service differs from reviewed version: '+name)
sys.path.insert(0,root)
import streaming
before=streaming.audit()
for key in ('robot_pids','modules','mounts','config_hashes'):
    if before[key]!=payload['audit'][key]:raise RuntimeError('Controller state changed: '+key)
with open(root+'/stream-manifest.json') as f:manifest=json.load(f)
for path,digest in manifest['sha256'].items():
    if streaming.sha(path)!=digest:raise RuntimeError('Camera helper integrity mismatch')
first=idle_devices()
kernel=output(['dmesg'])
if hashlib.sha256(kernel).hexdigest()!=payload['reviewed_kernel_sha256']:
    raise RuntimeError('Kernel log differs from inspected report; obtain fresh diagnostics')
if b'usb 1-2: USB disconnect, device number' not in kernel:
    raise RuntimeError('Expected disconnect evidence is absent')
time.sleep(5)
if idle_devices()!=first or output(['dmesg'])!=kernel:
    raise RuntimeError('USB or kernel state is still changing; inspect connection before retrying')
after=streaming.audit()
if not streaming.unchanged_for_recovery(before,after,'stationary'):
    raise RuntimeError('Controller is not stable; refusing restart')
backup=tempfile.mkdtemp(prefix='usb-disconnect-',dir=root)
with open(backup+'/dmesg.before.txt','wb') as f:f.write(kernel)
with open(backup+'/audit.before.json','w') as f:json.dump(before,f)
with open(backup+'/last-stream.json','w') as f:json.dump(payload['last_stream'],f)
# Stop/restart only this independent camera service. Do not reset USB, change
# kernel/audit policy or touch robot services. Preserve all failure evidence.
idle_devices()
subprocess.check_call(['/sbin/stop','fusion-camera'])
try:
    idle_devices()
finally:
    subprocess.check_call(['/sbin/start','fusion-camera'])
time.sleep(1)
status=output(['/sbin/status','fusion-camera'])
if b'start/running' not in status:raise RuntimeError('Camera service did not start')
current=streaming.audit()
if not streaming.unchanged_for_recovery(after,current,'stationary'):
    raise RuntimeError('Controller changed during restart; inspect before preview')
print('RECOVERY_RESULT='+json.dumps(dict(ok=True,backup=backup,usb=first,service=status.decode().strip())))
'''


def check_idle(app):
    state=app.get('status') or {}
    collection=state.get('collection') or {}
    if (state.get('state')!='idle' or state.get('recording')
            or any(collection.get(k) for k in ('armed','busy','recording'))
            or app.get('camera_thread_alive')):
        raise RuntimeError('请先停止遥操作、录制和预览，保持 App 打开')


def check_fault(status):
    last=status.get('last_stream') or {}
    if (status.get('version')!='2.3.2' or status.get('idle') is not True
            or status.get('fault')!=FAULT or last.get('native_exit')!=1
            or last.get('native_cleanup_timed_out') is not False
            or last.get('native_failure_reason')!='Camera open/start or negotiation failed'
            or 'Camera 1 UVC -99: Unknown error' not in last.get('native_diagnostics_tail',[])):
        raise RuntimeError('故障与已诊断的右腕 USB 掉线不一致，拒绝重启')


def app_diagnostics(base):
    with urllib.request.urlopen(base+'/tianji/diagnostics',timeout=6) as response:
        result=json.load(response)
    check_idle(result)
    return result


def verify_stream(client,seconds=10):
    counts=dict.fromkeys(('usb-1','usb-2','usb-4'),0)
    end=None
    for event in client.stream(seconds=seconds,mode='teleop_observation'):
        meta=event.metadata
        if meta.get('type')=='image':
            frame=event.frame().to_pil();frame.load()
            counts[meta['camera_id']]+=1
        if meta.get('type')=='end':end=meta
    if not all(counts.values()) or not end or end.get('ok') is not True or end.get('native_exit')!=0:
        raise RuntimeError('三路真实图像或正常收尾验证失败')
    return dict(frames=counts,end_ok=True,native_exit=0)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restart',action='store_true')
    parser.add_argument('--app-url',default='http://127.0.0.1:8011')
    args=parser.parse_args()
    if not args.restart:
        print('加 --restart 执行：检查空闲和 USB 稳定性、备份故障证据、仅重启相机服务，然后验证三轮三路取流。')
        return
    sys.path[:0]=[str(ROOT/'teleop_control/src'),str(ROOT/'fusion_camera_stream')]
    from generate_runtime_config import read_env
    config=Path(os.environ.get('XDG_CONFIG_HOME',str(Path.home()/'.config')))/'xiaokai-lab-data-collection'
    for path in (config/'.env',ROOT/'.env'):
        for key,value in read_env(path).items():os.environ.setdefault(key,value)
    from teleop_control.tianji_config import camera_host
    from teleop_control.tianji_camera_credentials import resolve_credentials
    from fusion_camera import FusionStreamClient
    host=camera_host()
    credentials=resolve_credentials(ROOT/'teleop_control/runtime/tianji')
    factory=lambda:FusionStreamClient(host,credentials=credentials,timeout=8)
    app=app_diagnostics(args.app_url)
    with factory() as client:
        status=client.status();check_fault(status)
    ended=[e['metadata'] for e in app.get('events',[]) if e.get('kind')=='preview_end']
    if not ended or ended[-1].get('error')!=FAULT or not ended[-1].get('audit_after'):
        raise RuntimeError('App 中缺少本次故障的审计证据，请勿关闭 App')
    diagnostic=json.loads((ROOT/'teleop_control/runtime/tianji/camera-controller-diagnostics.json').read_text())
    if diagnostic.get('dmesg',{}).get('code')!=0:raise RuntimeError('缺少有效的内核日志诊断')
    kernel_hash=hashlib.sha256(diagnostic['dmesg']['text'].encode()).hexdigest()
    payload=dict(sources=SOURCES,audit=ended[-1]['audit_after'],last_stream=status['last_stream'],reviewed_kernel_sha256=kernel_hash)
    app_diagnostics(args.app_url)
    script='PAYLOAD='+repr(base64.b64encode(json.dumps(payload).encode()).decode())+'\n'+REMOTE
    print('仅重启独立相机服务；不改审计规则、不复位 USB、不控制机械臂。请保持 App 待机，暂不点击检查设备。',flush=True)
    subprocess.run(['ssh','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8',
        'root@'+host,'/usr/bin/python2.7 -'],input=script,text=True,check=True)
    report=dict(verified=False,rounds=[])
    output=ROOT/'teleop_control/runtime/tianji/camera-restart-verification.json'
    output.parent.mkdir(parents=True,exist_ok=True)
    try:
        for index in range(3):
            app_diagnostics(args.app_url)
            with factory() as client:
                state=client.status()
                if state.get('idle') is not True or state.get('fault'):raise RuntimeError('服务仍有故障：'+str(state.get('fault')))
                report['rounds'].append(verify_stream(client))
            print('第 %d 轮三路图像及正常收尾通过'%(index+1),flush=True)
            time.sleep(3)
        report['verified']=True
    except Exception as exc:
        report['error']=str(exc)
        raise
    finally:output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('三轮验证通过。回 App 点击“检查设备”显示三路画面。验证报告：'+str(output))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        raise SystemExit('恢复未完成：'+str(exc))
