#!/usr/bin/env python3
"""Install reviewed camera recovery through the existing backup/rollback transaction."""
import base64
import json
from pathlib import Path
import subprocess
import sys
import urllib.request

import fix_tianji_camera_audit as installer

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_STREAM = '39dd9333be7825f286f95d833894d24229ec4fb5d361010709912b618147b9fe'
INVALID_FRAME = 'Invalid MJPEG frame or dimensions; stop'


def check_idle(app, status):
    state = app['status']
    if (state.get('state') != 'idle' or state.get('collection', {}).get('busy')
            or state.get('collection', {}).get('armed') or app.get('camera_thread_alive')):
        raise RuntimeError('请先停止遥操作和采集，并停止相机预览')
    if status.get('version') != '2.3.2' or status.get('idle') is not True:
        raise RuntimeError('相机服务必须是空闲的 v2.3.2')
    if status.get('fault'):
        last = status.get('last_stream') or {}
        reason = 'Native helper failed (exit 1): '+INVALID_FRAME
        if (status['fault'] != 'Stream stopped: '+reason
                or last.get('error') != reason or last.get('native_exit') != 1
                or last.get('native_cleanup_timed_out') is not False
                or last.get('native_failure_reason') != INVALID_FRAME):
            raise RuntimeError('故障与已检查的坏帧故障不同；拒绝清除')


def payload():
    source = ROOT/'fusion_data_collection/FusionDataCollector/controller_v22/streaming.py'
    return dict(expected=installer.EXPECTED, replacements=installer.REPLACEMENTS,
                expected_stream=EXPECTED_STREAM,
                verify_camera_idle=True,
                previous_policy_sha256=installer.PREVIOUS_POLICY_SHA256,
                policy=base64.b64encode(installer.POLICY.read_bytes()).decode('ascii'),
                stream_source=base64.b64encode(installer.patch_source(source.read_text()).encode()).decode('ascii'))


def main():
    sys.path.insert(0, str(ROOT/'fusion_camera_stream'))
    from fusion_camera import FusionStreamClient
    runtime = ROOT/'teleop_control/runtime/tianji'
    client = FusionStreamClient(credentials=runtime/'camera-client.json', timeout=8)
    try:
        with urllib.request.urlopen('http://127.0.0.1:8011/tianji/diagnostics', timeout=8) as response:
            app = json.load(response)
        status = client.status()
        check_idle(app, status)
        if status.get('stream_camera_selection') is not True:
            raise RuntimeError('请先运行 scripts/update_tianji_camera_stream.py，统一升级相机服务与原生取流程序')
        cameras = client.list_cameras()
        if {c['id'] for c in cameras if c.get('available')} != {'usb-1','usb-2','usb-4'}:
            raise RuntimeError('三路相机未全部识别')
        (runtime/'camera-recovery-install-before.json').write_text(json.dumps(
            dict(app=app, service=status, cameras=cameras), ensure_ascii=False, indent=2))
        script = 'PAYLOAD='+repr(base64.b64encode(json.dumps(payload()).encode()).decode('ascii'))+'\n'+installer.REMOTE
        result = subprocess.run(['ssh','-i',str(Path.home()/'.ssh/id_ed25519_192_168_1_190'),
            '-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
            '-o','ConnectTimeout=12','root@192.168.1.190','/usr/bin/python2.7 -'],
            input=script, text=True, capture_output=True, timeout=55)
        (runtime/'camera-recovery-install.log').write_text(result.stdout+result.stderr)
        print(result.stdout+result.stderr)
        result.check_returncode()
        after = client.status()
        (runtime/'camera-recovery-install-after.json').write_text(json.dumps(after, indent=2))
        if after.get('fault') or after.get('idle') is not True:
            raise RuntimeError('相机服务未恢复空闲')
        print('Camera recovery patch installed; camera service idle, fault cleared.')
    finally:
        client.close()


if __name__ == '__main__':
    main()
