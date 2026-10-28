"""Explicit PC-side checks for the current network wizard.

Importing this module does not connect, run sudo, or import the PICO SDK.
The SDK probe is isolated in a short-lived child process, with no robot client.
"""
import json
import math
from pathlib import Path
import subprocess
import sys
import time


PICO_OK = 'KUAVO_WIZARD_PICO_OK'


def prepare_network(root, namespace, service_port=7443):
    """Prepare only the selected PC namespace; never log in to the robot."""
    if type(service_port) is not int or not 1 <= service_port <= 65535:
        raise ValueError('wizard service port must be an integer from 1 to 65535')
    if namespace == 'current':
        return
    if namespace != 'kuavons':
        raise ValueError('unknown wizard network namespace')
    root = Path(root).resolve()
    subprocess.run(['sudo', '-v'], check=True)
    subprocess.run(['sudo', '-n', str(root/'scripts/kuavo-network'), 'up',
                    '--check-port', str(service_port)],
                   cwd=root, timeout=45, check=True)


def check_pico(root):
    """Check local SDK ownership, then bound init/read/close in a subprocess.

    subprocess.run kills and waits for the child on timeout. A successful exit
    alone is insufficient: the hidden entry must report the completed probe.
    """
    root = Path(root).resolve()
    occupied = subprocess.run(['ss', '-Htn', '( sport = :60061 or dport = :60061 )'],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3, check=True)
    if occupied.stdout.strip():
        raise RuntimeError('PICO 已被另一客户端使用；请先退出旧遥操作或预览。')
    try:
        result = subprocess.run([sys.executable, '-B', str(root/'apps/kuavo5_current_client.py'),
                '--wizard-check-pico'], cwd=root, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError('PICO 检查超过 15 秒，检查子进程已结束；未开始遥操作。') from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or '').strip()[-1500:]
        raise RuntimeError('PICO 输入检查失败；检查接收服务、USB 和 Send Data。'+
                           ('\n'+detail if detail else ''))
    for line in reversed(result.stdout.splitlines()):
        try:
            report = json.loads(line)
        except (ValueError, TypeError):
            continue
        if (isinstance(report, dict) and report.get('event') == PICO_OK
                and type(report.get('timestamp_advances')) is int
                and report['timestamp_advances'] >= 3
                and report.get('physical_robot_command_sent') is False):
            return report
    raise RuntimeError('PICO 检查没有返回有效完成结果；未开始遥操作。')


def probe_pico(*, factory=None, clock=time.monotonic, sleep=time.sleep, timeout_s=8.):
    """Probe fresh advancing PICO frames in the child; always release the SDK."""
    if (type(timeout_s) not in (int, float) or not math.isfinite(timeout_s)
            or not 0 < timeout_s <= 10.):
        raise ValueError('PICO probe timeout must be in (0, 10] seconds')
    if factory is None:
        from teleop.xr import XRoboToolkitInputAdapter
        factory = lambda: XRoboToolkitInputAdapter(stale_after_s=.1)
    xr = factory()
    try:
        xr.start()
        deadline = clock()+timeout_s
        previous = None
        advances = 0
        while clock() < deadline:
            frame = xr.read_frame()
            # A slow read cannot report success after the local budget expires.
            if clock() >= deadline:
                break
            if frame is None or frame.stale is not False:
                previous, advances = None, 0
            else:
                stamp = frame.device_timestamp_ns
                if type(stamp) is not int or stamp <= 0:
                    raise ValueError('PICO 设备时间无效；未开始遥操作。')
                if previous is not None:
                    if stamp < previous:
                        raise ValueError('PICO 设备时间倒退；未开始遥操作。')
                    if stamp > previous:
                        advances += 1
                previous = stamp
                if advances >= 3:
                    return dict(event=PICO_OK, timestamp_advances=advances,
                                physical_robot_command_sent=False)
            sleep(.01)
        raise TimeoutError('未收到持续更新的 PICO 输入；检查接收服务、USB 和 Send Data。')
    finally:
        xr.close()
