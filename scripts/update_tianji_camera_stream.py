#!/usr/bin/env python3
"""Install audited subset continuous camera capture; only restarts fusion-camera.

Run in a local terminal: OpenSSH asks for the controller password when needed.
Credentials never pass through this program or appear in its logs.
"""
import argparse, base64, hashlib, json, subprocess, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'fusion_camera_stream'))
sys.path.insert(0, str(ROOT/'teleop_control/src'))
from fusion_camera import FusionStreamClient
from teleop_control.tianji_camera_credentials import credentials_path
import fix_tianji_camera_audit as audit_patch

EXPECTED = {'server.py': ['3f5b3a4bd4188ed80ba36c1aab7ae92aba1e65b90b00fdce2695086aff7170fe', '9ce41154c1df0437b3d02b6df3068642f59b6a71987ca8e4b28c0db62b8e16d2', 'b2758734297a87678a343f5af3eab90e47969377e2a2a4e1b120a61d69909664', '94b6dc17c4d998076c21b1ac305ea56785ec97fff70e42fe83ac3d98d8253911'], 'streaming.py': ['ce689eb37bb8a4a4aee513db2556058c7a8700bf350f3bc4d6bc556f69082574', '39dd9333be7825f286f95d833894d24229ec4fb5d361010709912b618147b9fe', '5b52704aa33a0e947247d6f4414338b5dcf05ca08f9bf0b4b9cfbca27fc815bc']}
EXPECTED['server.py'].append('464b775c60fa48de2f8fab9192309584873d9476546d32177fc12c861c97cbec')
EXPECTED['streaming.py'].append('86bdb331e09a7da42bbb51a298b3e4d3029d2a37f7c2b8cd7a6a17c1428c2323')

EXPECTED['server.py'].append('540e8cabc09e579d90e4fefddd94db605cdce7e895d73af73c5f7e2fe2f819c3')
EXPECTED['streaming.py'].append('5a5a726f4a78a5728f521a1f8bb1183a1b50313c58beb2d5fcf8029fc489d328')

EXPECTED['server.py'].append('8ccfa04294e3edc008dc60e02c70dfaf2d3f5e5c729064116d6c0ad64fb1d460')
EXPECTED['streaming.py'].append('19ddebc8727b33e0d3fbeedbb4dd07a58961d3442012c152cf6879514efc76f9')
EXPECTED['server.py'].append('953d069e22b39427f65689213dc8e2a848aec39e722374d59650e83fa51bab2f')
EXPECTED['streaming.py'].append('212d2ee650f9c9c222e84af695ab29fa8aa24768e00416f495dd78d92ad5da11')
# Reviewed deployed inventory-recovery service, before camera controls.
EXPECTED['server.py'].append('5f1e3d8faf68f56278afb73e85af4ebc416378ffb2e8bc1c5a05b9a08dd80ffc')
EXPECTED['streaming.py'].append('79df00c97198d17834e6f4e4f77232fd97d306b0a6fa69df7a1114de109473c6')
EXPECTED['streaming.py'].append('96e6b7d9fc82aaeb5f7f2f0f846234310faf322f66333832b44fe680d7f2d8b8')
# Reviewed active-stream settings and independent reconnect release.
EXPECTED['server.py'].append('a3b4ece52c7651c156c53e6b423b6b2f876c6d8e8c9f628f3f3e07f8bc2281d5')
EXPECTED['streaming.py'].append('ecd4b8a6af05e9383fb6d77db22a5bd4f275be46f33211e84edd0a25d6c0f263')

# Reviewed predecessor before unlimited stream support.
EXPECTED['server.py'].append('79cc403b02ab97d29dc0842828f1425afaaa959d588400b504ca6f1c880f3d80')
EXPECTED['streaming.py'].append('e077dfe96620e17ef80a4725f379863271fbc90924b374b6f2f6c7f6b3bf8e60')

# Reviewed installed unlimited-stream release, before diagnostic-only timing.
# Streaming retrieved 2026-10-22; server digest reported by the upgrade gate.
# Both match the reviewed sources in the previous prepared update payload.
EXPECTED['server.py'].append('02cef217ef06ba7c29634cb52827fd90c9427e14cc5659f917a69b60b5b0892e')
EXPECTED['streaming.py'].append('9ba22a4b0bd851d11686adc322095f51742df642238833ca080c219f27b7781a')

# Reviewed diagnostic-only timing release, verified in controller diagnostics.
EXPECTED['server.py'].append('00027efd7fdab2c9806fa65c1f6924d71d91e27a8289be74ffd836a9039b44ba')
EXPECTED['streaming.py'].append('0f9eebbcfae5c021dff295530cfd37afc443f9c831c820bb1f3c327be4355747')

# Retrieved and reviewed read-only on 2026-10-22, before manual pipe recovery.
EXPECTED['server.py'].append('41839bb84ac82fe03a40a647e980ffa74fa2f82b2b8ed77ffadd31a1a8d69282')
EXPECTED['streaming.py'].append('67fc0a7866221d215b9b3c8524b1cca5056bb72bdde9911bbbe15366bf266dec')

REMOTE = r'''
from __future__ import print_function
import base64, hashlib, json, os, subprocess, tempfile, time, sys
payload = json.loads(base64.b64decode(PAYLOAD))
root = '/mnt/fusion-camera'
def digest(data): return hashlib.sha256(data).hexdigest()
def idle():
    with open(os.devnull, 'wb') as null:
        code = subprocess.call(['/usr/bin/pgrep', '-x', 'capture-three|capture-three-t|capture'], stdout=null)
    if code != 1: raise RuntimeError('Camera helper active or process check failed; refusing update')
if '/mnt/fusion-camera/server.py' not in open('/etc/init/fusion-camera.conf').read():
    raise RuntimeError('Unexpected camera service layout')
idle()
old = {name:open(os.path.join(root,name),'rb').read() for name in ('server.py','streaming.py','stream-manifest.json','camera_audit.py')}
modes = {name:os.stat(os.path.join(root,name)).st_mode & 511 for name in old}
for name, expected in payload['expected'].items():
    if digest(old[name]) not in expected:
        raise RuntimeError('Installed '+name+' is not a reviewed version; refusing overwrite. SHA256='+digest(old[name]))
sys.path.insert(0, root)
import server as service_api
import streaming as stream_api
from camera_audit import audit_changed
config = json.load(open(root+'/server.json'))
# Only inspect devices. Neither importing nor these checks starts USB capture.
available = [c for c in config['cameras'] if service_api.device_info(c)['available']]
if not available: raise RuntimeError('No cameras connected')
for camera in available:
    info = service_api.device_info(camera)
    if not service_api.interface_idle(camera): raise RuntimeError('Camera interface busy')
    with open(os.devnull,'wb') as null:
        code = subprocess.call(['/bin/fuser','/dev/bus/usb/%03d/%03d' % (info['bus'],info['address'])],stdout=null,stderr=null)
    if code != 1: raise RuntimeError('Camera busy or fuser failed')
audit_before = stream_api.audit()
manifest = json.loads(old['stream-manifest.json'])
for path, expected in manifest['sha256'].items():
    if digest(open(path,'rb').read()) != expected:
        raise RuntimeError('Existing integrity manifest mismatch: '+path)
if os.path.exists(root+'/capture-three-teleop'):
    old['capture-three-teleop'] = open(root+'/capture-three-teleop','rb').read()
    modes['capture-three-teleop'] = os.stat(root+'/capture-three-teleop').st_mode & 511
if os.path.exists(root+'/camera_controls.py'):
    old['camera_controls.py'] = open(root+'/camera_controls.py','rb').read()
    modes['camera_controls.py'] = os.stat(root+'/camera_controls.py').st_mode & 511
if os.path.exists(root+'/camera_color.py'):
    old['camera_color.py'] = open(root+'/camera_color.py','rb').read()
    modes['camera_color.py'] = os.stat(root+'/camera_color.py').st_mode & 511
release = tempfile.mkdtemp(prefix='teleop-v23-'+time.strftime('%Y%m%d-%H%M%S')+'-', dir=root+'/releases')
source = release+'/source'
for name, data in old.items():
    with open(release+'/'+name+'.before','wb') as f: f.write(data)
with open(release+'/status-before.json','w') as f: json.dump(payload['status'],f,indent=2)
with open(release+'/audit-before.json','w') as f: json.dump(audit_before,f,indent=2)
with open(release+'/dmesg-before.log','wb') as f:
    subprocess.check_call(['/bin/dmesg'],stdout=f)
for relative, encoded in payload['source'].items():
    if relative.startswith('/') or '..' in relative.split('/'):
        raise RuntimeError('Invalid source path')
    path = source+'/'+relative
    parent = os.path.dirname(path)
    if not os.path.isdir(parent): os.makedirs(parent)
    with open(path,'wb') as f: f.write(base64.b64decode(encoded))
new_helper = release+'/capture-three-teleop'
with open(release+'/build.log','wb') as log:
    try:
        subprocess.check_call(['/usr/bin/timeout','-k','2','60','/usr/bin/nice','-n','19',
                               '/usr/bin/taskset','-c','3','/bin/sh',
                               source+'/FusionDataCollector/controller_v22/build_teleop.sh',new_helper],
                              stdout=log,stderr=subprocess.STDOUT)
    except Exception:
        log.flush()
        print(open(release+'/build.log','rb').read()[-6000:])
        raise
print('Native reader built; compiler log: '+release+'/build.log')
subprocess.check_call([new_helper,'--self-test'])
target = root+'/capture-three-teleop'
if os.path.lexists(target) and 'capture-three-teleop' not in old:
    raise RuntimeError('Teleop helper changed during build; refusing overwrite')
manifest['sha256'][target] = digest(open(new_helper,'rb').read())
manifest['teleop_profile'] = payload['profile']
manifest['stream_camera_selection'] = True
manifest['camera_controls'] = True
manifest['stream_camera_controls'] = True
manifest['stream_joint_health'] = True
manifest['stream_unlimited'] = True
manifest['stream_transport_recovery'] = True
manifest['teleop_timing_policy'] = 'diagnostic_only'
manifest['teleop_queue_overflow_policy'] = 'drop_incoming'
manifest['sha256'][root+'/camera_controls.py'] = digest(base64.b64decode(
    payload['source']['FusionDataCollector/controller_v22/camera_controls.py']))
manifest['sha256'][root+'/camera_color.py'] = digest(base64.b64decode(
    payload['source']['FusionDataCollector/controller_v22/camera_color.py']))
manifest['sha256'][root+'/camera_audit.py'] = digest(base64.b64decode(
    payload['source']['FusionDataCollector/controller_v22/camera_audit.py']))
manifest['teleop_source_sha256'] = {
    name:digest(base64.b64decode(data)) for name,data in payload['source'].items()}
new = {'stream-manifest.json':json.dumps(manifest,sort_keys=True,indent=2).encode('ascii')}
for name in ('server.py','streaming.py','camera_color.py','camera_controls.py','camera_audit.py'):
    new[name] = open(source+'/FusionDataCollector/controller_v22/'+name,'rb').read()
    compile(new[name],name,'exec')
for name,data in new.items():
    with open(release+'/'+name+'.new','wb') as f: f.write(data)
    os.chmod(release+'/'+name+'.new',modes.get(name,0o600))
os.chmod(new_helper,0o700)
idle()
subprocess.check_call(['/sbin/stop','fusion-camera'])
try:
    idle()
    os.rename(new_helper,target)
    for name in new: os.rename(release+'/'+name+'.new',root+'/'+name)
    subprocess.check_call(['/sbin/start','fusion-camera'])
    time.sleep(1)
    if b'start/running' not in subprocess.check_output(['/sbin/status','fusion-camera']):
        raise RuntimeError('Updated camera service did not start')
    audit_after = stream_api.audit()
    with open(release+'/audit-after.json','w') as f: json.dump(audit_after,f,indent=2)
    if not stream_api.unchanged_for_recovery(audit_before,audit_after,'teleop_observation'):
        raise RuntimeError('Controller audit changed during camera update')
except Exception:
    subprocess.call(['/sbin/stop','fusion-camera'])
    for name,data in old.items():
        path = root+'/'+name+'.rollback'
        with open(path,'wb') as f: f.write(data)
        os.chmod(path,modes[name])
        os.rename(path,root+'/'+name)
    if 'capture-three-teleop' not in old and os.path.exists(target):
        os.rename(target,release+'/capture-three-teleop.failed')
    if 'camera_controls.py' not in old and os.path.exists(root+'/camera_controls.py'):
        os.rename(root+'/camera_controls.py',release+'/camera_controls.py.failed')
    if 'camera_color.py' not in old and os.path.exists(root+'/camera_color.py'):
        os.rename(root+'/camera_color.py',release+'/camera_color.py.failed')
    subprocess.check_call(['/sbin/start','fusion-camera'])
    raise
print('Subset continuous camera stream installed. Backup and reproducible sources: '+release)
'''

def source_files():
    base = ROOT/'fusion_data_collection'
    controller = base/'FusionDataCollector/controller_v22'
    paths = [controller/name for name in ('server.py','streaming.py','capture_teleop.c',
              'build_teleop.sh','camera_color.py','camera_controls.py')]
    vendor = base/'FusionCameraSDK/vendor'
    paths += sorted(vendor.rglob('*.h'))
    paths += sorted((vendor/'libuvc-0.0.6/src').glob('*.c'))
    files = {str(p.relative_to(base)):base64.b64encode(p.read_bytes()).decode('ascii') for p in paths}
    files['FusionDataCollector/controller_v22/streaming.py'] = base64.b64encode(
        audit_patch.patch_source((controller/'streaming.py').read_text()).encode()).decode('ascii')
    files['FusionDataCollector/controller_v22/camera_audit.py'] = base64.b64encode(
        (ROOT/'fusion_camera_stream/controller/camera_audit.py').read_bytes()).decode('ascii')
    return files


def reviewed_stream_fault(status):
    """Only the inspected startup failure with normal cleanup; never audit faults."""
    last = status.get('last_stream') or {}
    reason = last.get('native_failure_reason')
    if reason == 'Camera did not produce first frame after bounded startup retry; stop':
        # The controller must also have verified unchanged audit state and
        # complete cleanup. A matching error string alone is not sufficient.
        if (status.get('idle') is not True or status.get('recoverable_camera_fault') is not True
                or last.get('ok') is not False
                or last.get('observation_mode') != 'teleop_observation'
                or last.get('stop_reason') != 'stream_error'
                or last.get('native_termination') is not None):
            return False
    elif reason != 'Camera open/start or negotiation failed':
        return False
    error = 'Native helper failed (exit 1): ' + reason
    return (status.get('fault') in (error, 'Stream stopped: '+error)
            and last.get('error') == error and last.get('native_failure_reason') == reason
            and last.get('native_exit') == 1 and last.get('native_cleanup_timed_out') is False)


def reviewed_joint_fault(status):
    """Manually reviewed sampling delay; native video exited and cleaned normally."""
    last = status.get('last_stream') or {}
    error = 'Joint sampler delayed over 100 ms'
    return (status.get('fault') in (error, error+'; Audit changed: dmesg_sha256')
            and last.get('error') == status['fault'] and last.get('ok') is False
            and last.get('native_exit') == 0 and last.get('native_cleanup_timed_out') is False
            and last.get('native_termination') == 'error_cleanup'
            and not last.get('native_failure_reason')
            and last.get('observation_mode') == 'teleop_observation'
            and (last.get('joint_max_lateness_ns') or 0) > 100000000)


def reviewed_pipe_fault(status):
    """One-time upgrade of the inspected, cleanly exited legacy pipe failure."""
    last = status.get('last_stream') or {}
    reason = 'Frame pipe closed or blocked for 1 second; stop'
    error = 'Native helper failed (exit 1): '+reason
    return (status.get('version') == '2.3.2' and status.get('idle') is True
            and status.get('fault') in (error, 'Stream stopped: '+error)
            and last.get('ok') is False and last.get('error') == error
            and last.get('stop_reason') == 'stream_error'
            and last.get('observation_mode') == 'teleop_observation'
            and last.get('native_exit') == 1
            and last.get('native_cleanup_timed_out') is False
            and 'native_termination' in last and last['native_termination'] is None
            and last.get('native_failure_reason') == reason)


def status_details(status):
    """Expose upgrade blockers without credentials or large audit snapshots."""
    last = status.get('last_stream')
    fields = ('ok', 'error', 'stop_reason', 'observation_mode', 'native_exit',
              'native_cleanup_timed_out', 'native_termination',
              'native_failure_reason', 'joint_max_lateness_ns')
    return json.dumps({
        'version': status.get('version'), 'idle': status.get('idle'),
        'fault': status.get('fault'),
        'recoverable_camera_fault': status.get('recoverable_camera_fault'),
        'last_stream': {key: last.get(key) for key in fields} if isinstance(last, dict) else None,
    }, ensure_ascii=False, indent=2)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='192.168.1.190')
    parser.add_argument('--credentials',default=str(credentials_path()))
    parser.add_argument('--control-path')
    parser.add_argument('--status-only', action='store_true',
                        help='Only read camera service status; no SSH, update, recovery or restart')
    parser.add_argument('--recover-reviewed-snapshot-fault',action='store_true')
    parser.add_argument('--recover-reviewed-stream-fault',action='store_true',
                        help='Allow reviewed UVC negotiation failure or audited, recoverable first-frame timeout after cleanup')
    parser.add_argument('--recover-reviewed-dmesg-fault',action='store_true',
                        help='Allow a manually inspected idle dmesg audit failure; archive it without changing the failed recording')
    parser.add_argument('--recover-reviewed-queue-fault',action='store_true',
                        help='Allow an inspected and fully cleaned camera transport queue failure')
    parser.add_argument('--recover-reviewed-joint-fault',action='store_true',
                        help='Allow the inspected passive sampler delay after reviewing controller logs')
    parser.add_argument('--recover-reviewed-pipe-fault',action='store_true',
                        help='Allow the inspected legacy pipe timeout after normal cleanup; still checks integrity and idle resources')
    parser.add_argument('--prepare',type=Path,help='Write reviewed payload locally without connecting')
    args=parser.parse_args()
    files=source_files()
    expected={k:list(v) for k,v in EXPECTED.items()}
    for name in ('server.py','streaming.py'):
        expected[name].append(hashlib.sha256(base64.b64decode(files['FusionDataCollector/controller_v22/'+name])).hexdigest())
    payload=dict(source=files,expected=expected,profile=dict(format='mjpeg',width=1024,height=768,fps=30),status={})
    if args.prepare:
        args.prepare.write_text(json.dumps(payload))
        print('Prepared offline; no controller connection or device commands.')
        return
    with FusionStreamClient(args.host,credentials=args.credentials,timeout=8) as client:
        status=client.status()
    if args.status_only:
        print(status_details(status))
        return
    reviewed_snapshot_fault = (args.recover_reviewed_snapshot_fault and
        status.get('fault') == 'Worker failed (exit 1); manual inspection required' and
        status.get('last_stream') is None)
    reviewed_start_fault = args.recover_reviewed_stream_fault and reviewed_stream_fault(status)
    last = status.get('last_stream') or {}
    reviewed_dmesg_fault = (args.recover_reviewed_dmesg_fault
        and status.get('fault') == 'Audit changed: dmesg_sha256'
        and last.get('error') == status['fault'] and last.get('ok') is False
        and last.get('native_exit') == 0 and last.get('native_cleanup_timed_out') is False
        and last.get('native_termination') == 'client_stop'
        and not last.get('native_failure_reason'))
    queue_reason = 'Camera queue full; stop without silently dropping frames'
    queue_error = 'Native helper failed (exit 1): ' + queue_reason
    reviewed_queue_fault = (args.recover_reviewed_queue_fault
        and status.get('fault') in (queue_error,'Stream stopped: '+queue_error)
        and last.get('error') == queue_error and last.get('ok') is False
        and last.get('native_exit') == 1 and last.get('native_cleanup_timed_out') is False
        and last.get('native_failure_reason') == queue_reason)
    reviewed_sampler_fault = args.recover_reviewed_joint_fault and reviewed_joint_fault(status)
    reviewed_transport_fault = args.recover_reviewed_pipe_fault and reviewed_pipe_fault(status)
    if status.get('idle') is not True or (status.get('fault') and not (reviewed_snapshot_fault or reviewed_start_fault or reviewed_dmesg_fault or reviewed_queue_fault or reviewed_sampler_fault or reviewed_transport_fault)):
        reason = ('相机服务尚未空闲：请停止采集、退出 APP 和其他取流程序，等待取流释放后重试。'
                  if status.get('idle') is not True else
                  '相机服务存在故障记录：请先根据下方详情确认原因，再选择对应恢复方式。')
        raise RuntimeError('升级尚未执行。'+reason+'\n'+status_details(status))
    # Desktop and SDK must both remain closed for the entire update transaction.
    import fcntl
    data=Path.home()/'.local/share/xiaokai-lab-data-collection'
    locks=[]
    for path in [data/'app.lock']+list((data/'workspaces').glob('*/teleop_control/runtime/tianji/sdk.lock')):
        if path.exists():
            handle=path.open('a+');fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB);locks.append(handle)
    payload['status']=status
    encoded=base64.b64encode(json.dumps(payload).encode()).decode('ascii')
    command=['ssh','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8']
    if args.control_path:command+=['-S',args.control_path,'-o','BatchMode=yes']
    command+=['root@'+args.host,'/usr/bin/python2.7 -B -']
    script = REMOTE.replace('base64.b64decode(PAYLOAD)', 'base64.b64decode('+repr(encoded)+')', 1)
    compile(script, 'camera_update_remote.py', 'exec')
    subprocess.run(command,input=script,text=True,check=True,timeout=180)
    with FusionStreamClient(args.host,credentials=args.credentials,timeout=8) as client:
        after=client.status()
    if (after.get('idle') is not True or after.get('fault')
            or after.get('stream_camera_selection') is not True
            or after.get('camera_controls') is not True
            or after.get('stream_camera_controls') is not True
            or after.get('stream_joint_health') is not True
            or after.get('stream_transport_recovery') is not True
            or after.get('stream_unlimited') is not True
            or after.get('teleop_timing_policy') != 'diagnostic_only'
            or after.get('teleop_queue_overflow_policy') != 'drop_incoming'
            or after.get('camera_hotplug_recovery') is not True):
        raise RuntimeError('Updated camera service verification failed')
    print('相机服务升级完成：已支持“检查设备”恢复通过清理与审计检查的管道超时；保留不限时采集、队列质量记录、原生分辨率与硬件亮度。尚未验证实机出帧。')

if __name__=='__main__':
    main()
