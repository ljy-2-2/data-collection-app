#!/usr/bin/env python3
"""Capture one ZED Mini stereo frame via SSH; save two native 1920x1080 PNGs.

Run in a local terminal so OpenSSH can ask for the controller password.
No service restart, camera reset, persistent installation, or robot commands.
"""
import argparse
import base64
import datetime
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ('ctrl.c', 'ctrl-gen.c', 'device.c', 'diag.c', 'frame.c', 'init.c', 'stream.c', 'misc.c')

REMOTE = r'''
import base64,glob,json,os,shutil,subprocess,sys,tempfile
payload=json.loads(base64.b64decode(PAYLOAD))
def read(path):
    with open(path) as f: return f.read().strip()
devices=[]
for path in glob.glob('/sys/bus/usb/devices/*'):
    try:
        if read(path+'/idVendor')=='2b03' and read(path+'/idProduct')=='f682': devices.append(path)
    except IOError: pass
if len(devices)!=1: raise RuntimeError('Expected exactly one ZED Mini video device; found %d'%len(devices))
path=devices[0]
if float(read(path+'/speed'))<5000: raise RuntimeError('ZED video link is not USB SuperSpeed')
bus,address=int(read(path+'/busnum')),int(read(path+'/devnum'))
node='/dev/bus/usb/%03d/%03d'%(bus,address)
with open(os.devnull,'wb') as null:
    busy=subprocess.call(['/bin/fuser',node],stdout=null,stderr=null)
if busy!=1: raise RuntimeError('ZED video device is busy, or ownership could not be checked')
for interface in glob.glob(path+':*'):
    if os.path.exists(interface+'/driver'): raise RuntimeError('ZED video interface already has a driver; will not detach it')
folder=tempfile.mkdtemp(prefix='zed-one-frame-')
try:
    for name,data in payload['files'].items():
        if name.startswith('/') or '..' in name.split('/'): raise RuntimeError('Invalid source path')
        target=os.path.join(folder,name)
        if not os.path.isdir(os.path.dirname(target)): os.makedirs(os.path.dirname(target))
        with open(target,'wb') as f: f.write(base64.b64decode(data))
    cmd=['timeout','-k','2','60','nice','-n','19','taskset','-c','3','gcc','-std=gnu99','-O1','-pthread',
         '-DFUSION_STRICT_STREAM_QUERY','-I'+folder+'/vendor','-I'+folder+'/vendor/libuvc-0.0.6/include',
         folder+'/zed_snapshot.c']
    cmd += [folder+'/vendor/libuvc-0.0.6/src/'+name for name in payload['sources']]
    cmd += ['-l:libusb-1.0.so.0','-lrt','-o',folder+'/snapshot']
    subprocess.check_call(cmd,stdout=sys.stderr)
    subprocess.check_call(['timeout','-k','2','15','nice','-n','19','taskset','-c','3',
                           folder+'/snapshot',str(bus),str(address)])
finally:
    shutil.rmtree(folder)
'''


def payload():
    vendor = ROOT / 'fusion_data_collection/FusionCameraSDK/vendor'
    files = {'zed_snapshot.c': base64.b64encode((ROOT / 'scripts/zed_snapshot.c').read_bytes()).decode()}
    paths = [vendor / 'libusb.h', *sorted((vendor / 'libuvc-0.0.6/include').rglob('*.h'))]
    paths += [vendor / 'libuvc-0.0.6/src' / name for name in SOURCES]
    for path in paths:
        files['vendor/' + path.relative_to(vendor).as_posix()] = base64.b64encode(path.read_bytes()).decode()
    return base64.b64encode(json.dumps({'files': files, 'sources': SOURCES}).encode()).decode()


def save_images(raw, output):
    import cv2
    import numpy as np
    if len(raw) != 8 + 3840 * 1080 * 2 or raw[:4] != b'ZED1':
        raise RuntimeError('未收到完整的 3840×1080 YUYV 图像；没有生成图片。')
    image = cv2.cvtColor(np.frombuffer(raw, dtype=np.uint8, offset=8).reshape(1080, 3840, 2), cv2.COLOR_YUV2BGR_YUY2)
    output.mkdir(parents=True, exist_ok=False)
    for name, pixels in (('left', image[:, :1920]), ('right', image[:, 1920:])):
        ok, png = cv2.imencode('.png', pixels)
        if not ok: raise RuntimeError('PNG encoding failed')
        target = output / (name + '_1920x1080.png')
        target.write_bytes(png.tobytes())
        if cv2.imdecode(np.frombuffer(target.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR).shape != (1080, 1920, 3):
            raise RuntimeError('Saved PNG validation failed')
    (output / 'capture.json').write_text(json.dumps({
        'camera': 'ZED Mini', 'usb_id': '2b03:f682', 'source_format': 'YUYV',
        'source_resolution': [3840, 1080], 'eye_resolution': [1920, 1080],
        'negotiated_fps': 30, 'measured_fps': None, 'same_stereo_frame': True,
        'sequence': struct.unpack_from('<I', raw, 4)[0], 'raw_sha256': hashlib.sha256(raw[8:]).hexdigest(),
        'saved_at': datetime.datetime.now().astimezone().isoformat(),
        'processing': 'YUYV to BGR; split left/right; no resize, rectification or depth processing',
    }, ensure_ascii=False, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='192.168.1.190')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--ssh-control', help='Reuse an authenticated OpenSSH ControlPath')
    args = parser.parse_args()
    # Import before connecting so missing laptop dependencies do not waste a capture.
    import cv2, numpy  # noqa: F401
    desktop = subprocess.run(['xdg-user-dir', 'DESKTOP'], capture_output=True, text=True, check=True).stdout.strip()
    output = args.output or Path(desktop) / ('ZED左右单眼_' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S'))
    if output.exists(): raise RuntimeError('输出目录已存在，不覆盖：' + str(output))
    ssh = ['ssh', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=30',
           '-o', 'ConnectionAttempts=2',
           '-o', 'PreferredAuthentications=password,keyboard-interactive,publickey',
           '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2',
           'root@' + args.host, '/usr/bin/python2.7 -']
    script = 'PAYLOAD=' + repr(payload()) + '\n' + REMOTE
    if args.ssh_control:
        ssh[1:1] = ['-S', args.ssh_control, '-o', 'BatchMode=yes']
    print('只抓取 ZED Mini，一帧拆成左右各 1920×1080；请按 SSH 提示输入控制器密码。', flush=True)
    result = subprocess.run(ssh, input=script.encode(), stdout=subprocess.PIPE, timeout=240)
    if result.returncode == 255:
        raise RuntimeError('SSH 连接或认证失败，未收到抓拍结果；请查看上方 SSH 错误。')
    if result.returncode: raise RuntimeError('控制器抓拍失败，退出码 ' + str(result.returncode))
    save_images(result.stdout, output)
    print('已保存并核验：\n' + str(output / 'left_1920x1080.png') + '\n' + str(output / 'right_1920x1080.png'))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
        print('抓拍未完成：' + str(exc), file=sys.stderr)
        sys.exit(1)
