"""Explicit preview reconnection using a fresh controller camera check."""
import time


def require_idle(manager, *, allow_teleop=False):
    state = manager.snapshot(touch=False)
    collection = state.get("collection") or {}
    session = getattr(manager, 'camera_session', None)
    attached = getattr(session, 'attached', False)
    if attached and getattr(session, 'finished', None) is not None:
        attached = not session.finished.is_set()
    if (getattr(manager.controller, "hardware_enabled", True) is False
            or (not allow_teleop and state.get("state") != 'idle')
            or any(collection.get(k) for k in (("busy", "recording") if allow_teleop else ("armed", "busy", "recording")))
            or attached):
        raise RuntimeError("请先停止遥操作和采集，再重连本地相机预览")
    manager.require_other_idle()


def _require_not_cancelled(manager):
    if manager.camera_recovery_cancel.is_set():
        raise RuntimeError("本地相机预览重连已取消")


def restart_local_preview(manager, *, allow_teleop=False):
    require_idle(manager, allow_teleop=allow_teleop)
    _require_not_cancelled(manager)
    manager.camera_phase = "recovering"
    manager.camera_stop.set()
    thread, client = manager.camera_thread, manager.camera_client
    was_running = bool(thread and thread.is_alive())
    if was_running:
        if client:
            try:
                client.request_stop()
            except OSError:
                pass  # A broken connection still needs local cleanup.
        thread.join(timeout=6)
        if thread.is_alive():
            if client:
                client.close()
            thread.join(timeout=3)
        if thread.is_alive():
            raise RuntimeError("本地相机预览线程尚未退出，请稍后重新检查设备")
    if client:
        client.close()
    # Give the existing stream time to finish its normal cleanup.
    if was_running and manager.camera_recovery_cancel.wait(manager.camera_cooldown + 0.1):
        raise RuntimeError("本地相机预览重连已取消")
    require_idle(manager, allow_teleop=allow_teleop)
    _require_not_cancelled(manager)
    with manager.lock:
        manager.camera_thread = None
        manager.camera_client = None
        manager.camera_session = None
        manager.frames.clear()
        manager.camera_error = ""
        manager.camera_phase = "connecting"
    # Only an explicit Check Devices action reaches this code. The controller
    # checks current USB ownership and robot task stability. For a persistent
    # startup fault it may recover the dedicated camera USB controller, but only
    # with both arms disabled and no non-camera USB devices. No robot commands.
    with manager.new_camera_client() as fresh:
        # Upstart restarts an interrupted camera service after a short backoff.
        # Authentication/protocol errors remain errors; retry only transport loss.
        for attempt in range(3):
            try:
                status = fresh.status()
                break
            except OSError:
                if attempt == 2:
                    raise RuntimeError("相机服务暂时无法连接，请检查控制器网络或服务状态")
                if manager.camera_recovery_cancel.wait(1.0):
                    raise RuntimeError("本地相机预览重连已取消")
        if status.get('camera_manual_recheck') is True:
            require_idle(manager, allow_teleop=allow_teleop)
            _require_not_cancelled(manager)
            fresh.recheck_cameras()
            status = fresh.status()
            if status.get('fault') or status.get('idle') is not True:
                raise RuntimeError("相机现场检查未完成")
            return
        if status.get("fault"):
            if ('Frame pipe closed or blocked for 1 second; stop' in str(status['fault'])
                    and status.get('stream_transport_recovery') is not True):
                raise RuntimeError('控制器相机服务需要更新，才能通过“检查设备”恢复本次传输超时')
            if (status.get("idle") is not True
                    or status.get("camera_hotplug_recovery") is not True
                    or status.get("recoverable_camera_fault") is not True):
                raise RuntimeError("控制器相机故障尚不能安全重连：" + str(status["fault"]))
            require_idle(manager, allow_teleop=allow_teleop)
            _require_not_cancelled(manager)
            if status.get('camera_recovery_requires_manual') is True:
                fresh.recover_camera(manual=True)
            else:
                fresh.recover_camera()
            status = fresh.status()
            if status.get("fault") or status.get("idle") is not True:
                raise RuntimeError("控制器未完成相机重连")
    # The caller starts a fresh local receiver after checking cancellation.


def wait_for_preview(manager, timeout=35):
    """A manual check finishes on real fresh frames, not just a successful RPC."""
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if manager.camera_recovery_cancel.wait(.1):return None
        if time.monotonic()-manager.last_access>8:return None
        with manager.lock:
            ids=set(manager.camera_inventory)
            if ids and all(c in manager.frames and time.monotonic()-manager.frames[c][1]<manager.camera_frame_ttl for c in ids):
                return True
            alive=bool(manager.camera_thread and manager.camera_thread.is_alive())
            if not alive:
                return False if manager.camera_error else None
    return False


def startup_retry_needed(manager):
    return any(reason in str(manager.camera_error) for reason in (
        'Camera did not produce first frame after bounded startup retry; stop',
        'Camera has no new frame for 2 seconds; stop',
        'Camera open/start or negotiation failed'))
