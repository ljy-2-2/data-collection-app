"""Input-only preflight with optional PICO wake and local XR bridge recovery."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
from .galaxis import GalaxisHeadsetSetup


class KuavoHeadsetSetup(GalaxisHeadsetSetup):
    verified_serial = None

    def _device(self, result):
        serial = super()._device(result)
        if self.verified_serial and serial != self.verified_serial:
            self.last_error = 'PICO 设备已变化，请重新检查'
            return None
        return serial


def headset_status(headset=None, *, wake=False, prepare=False):
    headset = headset or KuavoHeadsetSetup()
    ready = headset._device(headset._run('devices',timeout=2)) if prepare else headset.probe_ready()
    if not ready:
        return dict(pico_ready=False,pico_detail=headset.last_error)
    serial = headset._serial
    maker = headset._run('-s',serial,'shell','getprop','ro.product.manufacturer',timeout=2)
    model = headset._run('-s',serial,'shell','getprop','ro.product.model',timeout=2)
    if (maker is None or model is None or maker.returncode or model.returncode or
            ('pico' not in (maker.stdout+model.stdout).lower() and model.stdout.strip()!='A9210')):
        return dict(pico_ready=False,pico_detail='已发现 ADB 设备，但无法核验为 PICO 头显')
    if prepare:
        headset.verified_serial = serial
        if not headset.ensure_started(quick=True, force_relaunch=False):
            return dict(pico_ready=False,pico_detail=headset.last_error)
    if wake and not headset.wake():
        return dict(pico_ready=False,pico_detail=headset.last_error or 'PICO 唤醒失败')
    mapping = headset._run('-s',serial,'reverse','--list',timeout=2)
    tunnel = bool(mapping is not None and mapping.returncode==0 and any(
        row.split()[-2:]==['tcp:63901','tcp:63901'] for row in mapping.stdout.splitlines()))
    power = headset._run('-s',serial,'shell','dumpsys','power',timeout=2)
    awake = None
    if power is not None and power.returncode==0:
        if 'mWakefulness=Asleep' in power.stdout:awake=False
        elif 'mWakefulness=Awake' in power.stdout:awake=True
    return dict(pico_ready=tunnel,pico_awake=awake,pico_detail='PICO 已授权、Galaxis VR 已启动、USB 数据通道已就绪' if tunnel else
        'PICO 已授权且应用已启动，但缺少 USB 63901 → 63901 数据通道；请完成头显连接设置')


def check_input(source, *, clock=time.monotonic, sleep=time.sleep, duration=3.):
    """Ignore the initial cached sample; require consecutive advancing data."""
    result=dict(vr_ready=False,vr_detail='未收到持续更新的 VR 数据；请确认 WORK、Head、Controllers 和 Send Data 已开启')
    last=None; advances=0; first=None
    try:
        source.start()
        deadline=clock()+duration
        while clock()<deadline:
            frame=source.read_frame()
            valid = frame is not None and not frame.stale and all(
                .25 <= sum(float(q)**2 for q in pose.orientation_xyzw) <= 2.25
                for pose in (frame.head_pose,frame.left_controller.pose,frame.right_controller.pose))
            if valid:
                stamp=frame.device_timestamp_ns
                if last is not None and stamp>last:
                    if first is None:first=clock()
                    advances+=1
                    if advances>=3 and clock()-first>=.15:
                        result=dict(vr_ready=True,vr_detail='已收到持续更新的头显和双手柄输入',xr_samples=advances)
                        break
                elif last is not None and stamp<last:
                    advances=0;first=None
                last=stamp
            else:
                advances=0;first=None;last=None
            sleep(.02)
    except Exception as exc:
        result=dict(vr_ready=False,vr_detail='VR 输入检查失败：'+str(exc))
    finally:
        try:source.close()
        except Exception as exc:result=dict(vr_ready=False,vr_cleanup_failed=True,vr_detail='VR 输入检查未正常释放：'+str(exc))
    return result


def unavailable(message):
    return dict(vr_checked=True,pico_ready=False,vr_ready=False,pico_sdk_available=False,
                pico_detail=message,vr_detail=message)


def check_with_recovery(input_factory, service):
    """Used only by an idle device check; release SDK before replacing bridge."""
    try:
        service.ensure_started()
        result = check_input(input_factory())
        if result['vr_ready'] or result.get('vr_cleanup_failed'):
            return result
        service.restart()
        result = check_input(input_factory())
        result['vr_recovery_attempted'] = True
        if not result['vr_ready']:
            result['vr_detail'] = '已恢复本机 VR 接收服务，' + result['vr_detail']
        return result
    except Exception as exc:
        return dict(vr_ready=False,vr_detail='本机 VR 接收服务恢复失败：'+str(exc))


def probe_subprocess():
    """A native SDK failure cannot take down the robot status check."""
    try:
        result=subprocess.run([sys.executable,'-u','-m','teleop_control.kuavo_vr','--wake','--recover','--prepare'],
                              capture_output=True,text=True,timeout=200)
        for line in reversed(result.stdout.splitlines()):
            if line.startswith('KUAVO_VR:'):
                value=json.loads(line[9:])
                if result.returncode==0:return value
        return unavailable('头显检查进程异常退出；请检查本机 XR SDK 和接收服务')
    except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
        return unavailable('头显检查未完成：'+str(exc))


def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--wake',action='store_true',help='Wake the authorized PICO only')
    parser.add_argument('--recover',action='store_true',help='Recover the local XR receiver if idle input check fails')
    parser.add_argument('--prepare',action='store_true',help='Prepare the verified PICO app and USB tunnel')
    args=parser.parse_args()
    headset=KuavoHeadsetSetup()
    result=headset_status(headset,wake=args.wake,prepare=args.prepare)
    result.update(vr_checked=True,pico_sdk_available=importlib.util.find_spec('xrobotoolkit_sdk') is not None)
    if result['pico_sdk_available']:
        sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'teleop_control/vendor/kuavo_runtime/src'))
        from teleop.xr import XRoboToolkitInputAdapter
        factory=lambda:XRoboToolkitInputAdapter(stale_after_s=.1)
        if args.recover and result['pico_ready']:
            from .runtime_paths import resolve_xr_service
            from .xr_service import XrServiceLauncher
            result.update(check_with_recovery(factory,XrServiceLauncher(resolve_xr_service(),timeout_seconds=8)))
        else:
            result.update(check_input(factory()))
    else:
        result.update(vr_ready=False,vr_detail='当前 Python 缺少 PICO xrobotoolkit_sdk')
    if result['pico_ready']:
        # Re-read after checking/recovery: proximity sleep can return quickly.
        power=headset._run('-s',headset._serial,'shell','dumpsys','power',timeout=2)
        if power is not None and power.returncode==0:
            result['pico_awake']=(True if 'mWakefulness=Awake' in power.stdout else
                                  False if 'mWakefulness=Asleep' in power.stdout else None)
    if not result['vr_ready'] and result.get('pico_awake') is False:
        result['vr_detail']='PICO 当前处于休眠，未收到有效 VR 输入；请戴上并唤醒头显，再确认 WORK / Head / Controllers / Send Data'
    # The native SDK writes to stdout without a trailing newline on deinit.
    print('\nKUAVO_VR:'+json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
