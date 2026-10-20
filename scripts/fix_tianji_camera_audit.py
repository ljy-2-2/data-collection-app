#!/usr/bin/env python3
"""Patch only the reviewed camera audit; run --install in the user's terminal."""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / 'fusion_camera_stream/controller/camera_audit.py'
# Reviewed SDK v2.3.2, including the user's latest camera and recovery changes.
EXPECTED = 'cb3ee09192f785ef6645b0ab1eba1176a0c9576c386c75378a22ec80aab5cca3'
PREVIOUS_POLICY_SHA256 = 'a23502ce0c09e6b295526bfd4285b79ab0b414ec69bb09b2907534cf97b99dda'
REPLACEMENTS = [
    ('from joints import JointReader\n',
     'from joints import JointReader\nfrom camera_audit import dmesg_fingerprint, audit_changed\n'),
    ("out['dmesg_sha256'] = hashlib.sha256(subprocess.check_output(['dmesg'])).hexdigest()",
     "out.update(dmesg_fingerprint(subprocess.check_output(['dmesg'])))"),
    ('if before[key] != after[key]:', 'if audit_changed(before, after, key, mode):'),
]

# Executed by the controller's Python 2.7. No robot SDK imports or motor commands.
REMOTE = r'''
import base64,hashlib,json,os,stat,subprocess,sys,tempfile,time
payload = json.loads(base64.b64decode(PAYLOAD))
root = '/mnt/fusion-camera'
path = root + '/streaming.py'
policy_path = root + '/camera_audit.py'
def read(p):
    with open(p, 'rb') as f: return f.read()
def idle():
    with open(os.devnull, 'wb') as null:
        if subprocess.call(['/usr/bin/pgrep','-x','capture|capture-three|capture-three-t'], stdout=null) != 1:
            raise RuntimeError('Camera capture is running; stop collection first')
def checked_file(p):
    s = os.lstat(p)
    if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:
        raise RuntimeError('Unexpected file owner, permissions or symlink: '+p)
def write(p, data, mode):
    with open(p, 'wb') as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    os.chmod(p, mode)
if os.geteuid() != 0: raise RuntimeError('Controller root login required')
idle()
if b'/mnt/fusion-camera/server.py' not in read('/etc/init/fusion-camera.conf'):
    raise RuntimeError('Unexpected camera service layout')
checked_file(path)
old = read(path)
policy = base64.b64decode(payload['policy'])
old_policy = None
if os.path.lexists(policy_path):
    checked_file(policy_path)
    old_policy = read(policy_path)
    if (old_policy != policy and hashlib.sha256(old_policy).hexdigest()
            != payload.get('previous_policy_sha256')):
        raise RuntimeError('Existing camera_audit.py differs; refusing overwrite')
if payload.get('stream_source'):
    new = base64.b64decode(payload['stream_source'])
    if old == new and old_policy == policy:
        print('Camera patch already installed; no restart performed')
        sys.exit(0)
    if hashlib.sha256(old).hexdigest() != payload['expected_stream']:
        raise RuntimeError('Installed streaming.py differs from reviewed recovery baseline')
elif b'from camera_audit import dmesg_fingerprint, audit_changed' in old:
    if not os.path.exists(policy_path): raise RuntimeError('Audit policy file is missing')
    original = old
    for before, after in reversed(payload['replacements']):
        before, after = before.encode('utf-8'), after.encode('utf-8')
        if original.count(after) != 1: raise RuntimeError('Unexpected existing patch')
        original = original.replace(after, before)
    if hashlib.sha256(original).hexdigest() != payload['expected']:
        raise RuntimeError('Patched service has additional changes; inspect manually')
    if old_policy == policy:
        print('Audit patch already installed; no restart performed')
        sys.exit(0)
    new = old  # Upgrade only the exact known policy; streaming stays identical.
else:
    if hashlib.sha256(old).hexdigest() != payload['expected']:
        raise RuntimeError('Installed streaming.py differs from reviewed v2.3.2; refusing overwrite. SHA256='+hashlib.sha256(old).hexdigest())
    new = old
    for before, after in payload['replacements']:
        before, after = before.encode('utf-8'), after.encode('utf-8')
        if new.count(before) != 1: raise RuntimeError('Audit patch target is not unique')
        new = new.replace(before, after)
compile(new, path, 'exec')
compile(policy, policy_path, 'exec')
if payload.get('verify_camera_idle'):
    sys.path.insert(0, root)
    import server, streaming
    with open(root+'/stream-manifest.json') as f: manifest = json.load(f)
    for p, digest in manifest['sha256'].items():
        if streaming.sha(p) != digest: raise RuntimeError('Camera helper integrity mismatch')
    for port in ('1-1','1-2','1-4'):
        if not server.interface_idle({'usb_path':port}):
            raise RuntimeError('Camera interface did not return to idle: '+port)
        base = '/sys/bus/usb/devices/'+port
        device = '/dev/bus/usb/%03d/%03d' % (int(read(base+'/busnum')), int(read(base+'/devnum')))
        with open(os.devnull, 'wb') as null:
            if subprocess.call(['/bin/fuser',device],stdout=null,stderr=null) != 1:
                raise RuntimeError('Camera is occupied or ownership check failed')
    rt = streaming.rt_snapshot(streaming.audit()['xenomai'])
    if any(row[4] for row in rt.values()) or rt['FX_RtTask'][2]:
        raise RuntimeError('RT task fault counters require inspection')
backup = tempfile.mkdtemp(prefix='audit-servo-', dir=root)
write(backup+'/streaming.py.before', old, 0o600)
if old_policy is not None: write(backup+'/camera_audit.py.before', old_policy, 0o600)
write(backup+'/dmesg.before.txt', subprocess.check_output(['dmesg']), 0o600)
write(backup+'/streaming.py.new', new, os.stat(path).st_mode & 0o777)
write(backup+'/camera_audit.py.new', policy, 0o644)
had_policy = os.path.exists(policy_path)
print('Backup: '+backup)
sys.stdout.flush()
idle()
subprocess.check_call(['/sbin/stop','fusion-camera'])
try:
    idle()
    os.rename(backup+'/camera_audit.py.new', policy_path)
    os.rename(backup+'/streaming.py.new', path)
    # Remove our module bytecode so Python 2 never reuses a same-timestamp cache.
    for p in (path+'c', path+'o', policy_path+'c', policy_path+'o'):
        if os.path.isfile(p): os.unlink(p)
    subprocess.check_call([sys.executable, '-c',
        'import streaming; from camera_audit import POLICY; assert POLICY == "known-servo-transitions-v3"'], cwd=root)
    subprocess.check_call(['/sbin/start','fusion-camera'])
    time.sleep(1)
    result = subprocess.check_output(['/sbin/status','fusion-camera'])
    if b'start/running' not in result: raise RuntimeError('Camera service did not start')
except Exception:
    subprocess.call(['/sbin/stop','fusion-camera'])
    write(backup+'/streaming.py.rollback', old, os.stat(path).st_mode & 0o777)
    os.rename(backup+'/streaming.py.rollback', path)
    for p in (path+'c', path+'o'):
        if os.path.isfile(p): os.unlink(p)
    if not had_policy:
        for p in (policy_path, policy_path+'c', policy_path+'o'):
            if os.path.isfile(p): os.unlink(p)
    else:
        write(backup+'/camera_audit.py.rollback', old_policy, 0o644)
        os.rename(backup+'/camera_audit.py.rollback', policy_path)
        for p in (policy_path+'c', policy_path+'o'):
            if os.path.isfile(p): os.unlink(p)
    subprocess.check_call(['/sbin/start','fusion-camera'])
    raise
print('Camera audit patched; independent camera service restarted. Backup: '+backup)
print(result.strip())
'''


def patch_source(source):
    for before, after in REPLACEMENTS:
        if source.count(before) != 1:
            raise ValueError('Audit patch target is not unique')
        source = source.replace(before, after)
    return source


def check_idle(app, status):
    if app.get('state') != 'idle' or (app.get('collection') or {}).get('busy'):
        raise RuntimeError('请先在 App 停止控制和采集，保持设备连接。')
    if status.get('version') != '2.3.2' or status.get('idle') is not True:
        raise RuntimeError('仅支持空闲的相机服务 v2.3.2。')
    if status.get('fault'):
        last = status.get('last_stream') or {}
        if (status['fault'] != 'Audit changed: dmesg_sha256'
                or last.get('native_exit') != 0
                or last.get('native_cleanup_timed_out')
                or last.get('native_failure_reason')):
            raise RuntimeError('故障不是已核对的日志变化，请先检查控制器。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args()
    if not args.install:
        print('只修正运动采集的已知伺服启停日志误报；保留其它审计、原始日志哈希及静止模式检查。')
        print('在本机终端加 --install 安装；SSH 密码由 OpenSSH 读取，不会保存。')
        return
    sys.path.insert(0, str(ROOT/'fusion_camera_stream'))
    from fusion_camera import FusionStreamClient
    with urllib.request.urlopen('http://127.0.0.1:8011/tianji/status', timeout=5) as response:
        app = json.load(response)
    client = FusionStreamClient('192.168.1.190', credentials=ROOT/'teleop_control/runtime/tianji/camera-client.json', timeout=5)
    try:
        check_idle(app, client.status())
        payload = dict(expected=EXPECTED, replacements=REPLACEMENTS,
                       previous_policy_sha256=PREVIOUS_POLICY_SHA256,
                       policy=base64.b64encode(POLICY.read_bytes()).decode('ascii'))
        script = 'PAYLOAD='+repr(base64.b64encode(json.dumps(payload).encode()).decode('ascii'))+'\n'+REMOTE
        print('将备份内核日志和 streaming.py，安装有限审计修正，仅重启 fusion-camera。', flush=True)
        subprocess.run(['ssh','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8',
                        'root@192.168.1.190','/usr/bin/python2.7 -'], input=script, text=True, check=True)
        result = client.status()
        if result.get('fault') or result.get('idle') is not True:
            raise RuntimeError('相机服务仍未就绪：'+str(result.get('fault') or 'busy'))
        print('服务已恢复空闲，故障已清除。回 App 点击“检查设备”重新打开预览。')
    finally:
        client.close()


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.exit('未完成安装：'+str(exc))
