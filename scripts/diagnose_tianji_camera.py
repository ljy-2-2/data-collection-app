#!/usr/bin/env python3
"""Read-only diagnostics, with an explicit guarded main-camera USB reset option."""
import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'teleop_control/runtime/tianji/camera-controller-diagnostics.json'

# Python 2.7. Default is read-only. Reset requires an explicit local flag,
# idle App/service checks, and fresh remote identity/ownership checks.
REMOTE = r'''
import base64,glob,hashlib,json,os,subprocess,time
def read(path, limit=1048576):
    try:
        with open(path,'rb') as f: return f.read(limit).decode('utf-8','replace')
    except IOError as e: return 'UNAVAILABLE: '+str(e)
def command(args):
    try:
        p = subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        text = p.communicate()[0].decode('utf-8','replace')
        return dict(code=p.returncode,text=text)
    except OSError as e: return dict(error=str(e))
def main_camera_node():
    path = '/sys/bus/usb/devices/1-1'
    if (read(path+'/idVendor').strip(),read(path+'/idProduct').strip()) != ('0bda','5883'):
        raise RuntimeError('Port 1-1 does not contain the expected main camera')
    if read(path+'/authorized').strip() != '1':
        raise RuntimeError('Main camera is not authorized')
    if read(path+'/busnum').strip() != '1':
        raise RuntimeError('Unexpected USB bus')
    interfaces = glob.glob(path+':*')
    if not interfaces: raise RuntimeError('Camera interfaces are missing')
    for interface in interfaces:
        if os.path.exists(interface+'/driver'):
            raise RuntimeError('Camera interface has a kernel driver; refusing reset')
        if read(interface+'/bAlternateSetting').strip() != '0':
            raise RuntimeError('Camera interface is active; refusing reset')
    address = int(read(path+'/devnum').strip())
    if not 2 <= address <= 127: raise RuntimeError('Invalid camera device address')
    node = '/dev/bus/usb/001/%03d' % address
    if command(['/bin/fuser',node]).get('code') != 1:
        raise RuntimeError('Camera is owned by another process or ownership check failed')
    return node
def reset_main_camera():
    import fcntl,signal,stat
    outcome = dict(ok=False,usb_path='1-1',reset_completed=False)
    stopped = False
    def expired(signum,frame): raise RuntimeError('USB reset timed out; no automatic retry')
    try:
        if os.geteuid() != 0: raise RuntimeError('Controller root login required')
        main_camera_node()
        helpers = command(['/usr/bin/pgrep','-x','capture|capture-three|capture-three-t'])
        if helpers.get('code') != 1: raise RuntimeError('Camera helper is still running')
        robot_before = command(['/usr/bin/pgrep','-x','FXProducer|FXPSI|FXTcpFileServer|EtherCAT-OP'])
        if robot_before.get('code') != 0: raise RuntimeError('Cannot verify robot process identities')
        stop = command(['/sbin/stop','fusion-camera'])
        if stop.get('code') != 0: raise RuntimeError('Camera service did not stop: '+stop.get('text',''))
        stopped = True
        if command(['/usr/bin/pgrep','-x','capture|capture-three|capture-three-t']).get('code') != 1:
            raise RuntimeError('Camera helper did not exit')
        node = main_camera_node()
        fd = os.open(node,os.O_RDWR | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISCHR(info.st_mode) or os.major(info.st_rdev) != 189
                    or os.minor(info.st_rdev) != int(node.rsplit('/',1)[1])-1):
                raise RuntimeError('Not a USB device node')
            old_handler = signal.signal(signal.SIGALRM,expired)
            signal.alarm(10)
            try:
                # USBDEVFS_RESET = _IO('U',20), on this verified camera only.
                fcntl.ioctl(fd,0x5514,0)
                outcome['reset_completed'] = True
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM,old_handler)
        finally: os.close(fd)
        time.sleep(2)
        outcome['device_after'] = main_camera_node()
        robot_after = command(['/usr/bin/pgrep','-x','FXProducer|FXPSI|FXTcpFileServer|EtherCAT-OP'])
        outcome['robot_processes_unchanged'] = robot_before == robot_after
        if not outcome['robot_processes_unchanged']:
            raise RuntimeError('Robot process identities changed; inspect controller')
        outcome['ok'] = True
    except Exception as exc:
        outcome['error'] = str(exc)
    finally:
        if stopped:
            outcome['service_start'] = command(['/sbin/start','fusion-camera'])
            time.sleep(1)
            outcome['service_after'] = command(['/sbin/status','fusion-camera'])
            if (outcome['service_start'].get('code') != 0 or
                    'start/running' not in outcome['service_after'].get('text','')):
                outcome['ok'] = False
                outcome['service_error'] = 'Camera service did not restart'
        outcome['dmesg_after'] = command(['dmesg'])
    return outcome
result = dict(dmesg=command(['dmesg']), service=command(['/sbin/status','fusion-camera']),
              processes=command(['ps','-e','-o','pid,comm']),
              uname=command(['uname','-a']))
result['manifest'] = read('/mnt/fusion-camera/stream-manifest.json')
result['camera_sources'] = {}
for name in ('streaming.py','server.py','camera_audit.py'):
    path = '/mnt/fusion-camera/'+name
    try:
        with open(path,'rb') as f: data=f.read()
        result['camera_sources'][name] = dict(sha256=hashlib.sha256(data).hexdigest())
    except IOError as e: result['camera_sources'][name] = dict(error=str(e))
result['usb'] = {}
for name in ('1-1','1-2','1-4'):
    path = '/sys/bus/usb/devices/'+name
    info = dict((key,read(path+'/'+key).strip()) for key in
        ('idVendor','idProduct','busnum','devnum','speed','bcdDevice','bcdUSB',
         'bConfigurationValue','authorized','product','manufacturer',
         'power/control','power/runtime_status','power/autosuspend'))
    info['path'] = os.path.realpath(path)
    try:
        with open(path+'/descriptors','rb') as f:
            info['descriptors_base64'] = base64.b64encode(f.read(65536)).decode('ascii')
    except IOError as e: info['descriptor_error'] = str(e)
    info['interfaces'] = {}
    for interface in glob.glob(path+':*'):
        entry = dict((key,read(interface+'/'+key).strip()) for key in
                     ('bInterfaceClass','bInterfaceNumber','bAlternateSetting'))
        entry['driver'] = os.path.realpath(interface+'/driver') if os.path.exists(interface+'/driver') else None
        info['interfaces'][os.path.basename(interface)] = entry
    try:
        device = '/dev/bus/usb/%03d/%03d' % (int(info['busnum']),int(info['devnum']))
        info['owners'] = command(['/bin/fuser','-v',device])
    except ValueError: pass
    result['usb'][name] = info
def counters():
    return dict((key,read('/proc/'+key)) for key in
                ('interrupts','softirqs','stat','loadavg','uptime','xenomai/stat'))
result['counters_before'] = counters()
time.sleep(1)
result['counters_after'] = counters()
if globals().get('RESET_MAIN_CAMERA',False):
    result['reset'] = reset_main_camera()
print(json.dumps(result,ensure_ascii=True))
'''


def check_reset_allowed(app, status):
    if app.get('state') != 'idle' or (app.get('collection') or {}).get('busy'):
        raise RuntimeError('请先停止 App 中的控制和采集，保持 App 打开。')
    if status.get('idle') is not True or status.get('version') != '2.3.2':
        raise RuntimeError('相机服务不为空闲或版本不符合本次诊断。')
    reason = 'Native helper failed (exit 1): Camera did not produce first frame after bounded startup retry; stop'
    last = status.get('last_stream') or {}
    snapshot_after_same_timeout = (
        status.get('fault') == 'Worker failed (exit 1); manual inspection required'
        and last.get('error') == reason
        and last.get('native_failure_reason') == 'Camera did not produce first frame after bounded startup retry; stop')
    if status.get('fault') not in (reason, 'Stream stopped: '+reason) and not snapshot_after_same_timeout:
        raise RuntimeError('当前不是已诊断的首帧超时，拒绝复位。')
    if last.get('native_exit') != 1 or last.get('native_cleanup_timed_out'):
        raise RuntimeError('上次采集未正常清理，拒绝复位。')


def camera_client():
    sys.path.insert(0, str(ROOT/'fusion_camera_stream'))
    from fusion_camera import FusionStreamClient
    return FusionStreamClient('192.168.1.190',
        credentials=ROOT/'teleop_control/runtime/tianji/camera-client.json',timeout=5)


def verify_frames(client):
    counts = dict.fromkeys(('usb-1','usb-2','usb-4'),0)
    result = dict(ok=False,frames=counts)
    try:
        status = client.status()
        if status.get('fault') or status.get('idle') is not True:
            raise RuntimeError('服务复位后未就绪：'+str(status.get('fault') or 'busy'))
        for event in client.stream(seconds=3,mode='teleop_observation'):
            if event.metadata.get('type') == 'image':
                counts[event.metadata['camera_id']] += 1
            elif event.metadata.get('type') == 'end':
                result['end_ok'] = event.metadata.get('ok') is True
        result['ok'] = all(counts.values()) and result.get('end_ok',False)
    except Exception as exc:
        result['error'] = str(exc)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reset-main-camera',action='store_true',help='空闲时仅复位主相机 USB，然后做一次三路取流验证')
    args = parser.parse_args()
    if args.reset_main_camera:
        with urllib.request.urlopen('http://127.0.0.1:8011/tianji/status',timeout=5) as response:
            app = json.load(response)
        client = camera_client()
        try: check_reset_allowed(app,client.status())
        finally: client.close()
        print('将检查占用，仅复位主相机 1-1 的 USB 设备并重启相机服务；完成后测试一次三路取流。请保持控制和采集停止。',flush=True)
    else:
        print('只读检查 USB、内核日志及接口占用；不会启动相机或控制机器人。', flush=True)
    result = subprocess.run(
        ['ssh', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=8',
         'root@192.168.1.190', '/usr/bin/python2.7 -'],
        input='RESET_MAIN_CAMERA='+repr(args.reset_main_camera)+'\n'+REMOTE,
        text=True, stdout=subprocess.PIPE, check=True,
    )
    report = json.loads(result.stdout)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    if args.reset_main_camera:
        reset = report.get('reset') or {}
        if reset.get('ok'):
            client = camera_client()
            try: report['verification'] = verify_frames(client)
            finally: client.close()
            OUTPUT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
            print('取流验证：'+json.dumps(report['verification'],ensure_ascii=False))
        else:
            print('主相机复位未完成：'+json.dumps(
                {key:reset.get(key) for key in ('error','service_error','reset_completed','service_after')},
                ensure_ascii=False))
    print('诊断已保存：'+str(OUTPUT))
    print('请告知助手“诊断完成”，无需复制整份日志。')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SystemExit('诊断未完成：'+str(exc))
