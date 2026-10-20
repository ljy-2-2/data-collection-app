"""Idle-only preview reconnection and one audited camera-service retry."""


def require_idle(manager):
    state = manager.snapshot(touch=False)
    collection = state.get("collection") or {}
    if (getattr(manager.controller, "hardware_enabled", True) is False
            or state.get("state") != "idle"
            or any(collection.get(k) for k in ("armed", "busy", "recording"))):
        raise RuntimeError("请先停止遥操作和采集，再重连本地相机预览")
    manager.require_other_idle()


def _require_not_cancelled(manager):
    if manager.camera_recovery_cancel.is_set():
        raise RuntimeError("本地相机预览重连已取消")


def restart_local_preview(manager):
    require_idle(manager)
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
    require_idle(manager)
    _require_not_cancelled(manager)
    with manager.lock:
        manager.camera_thread = None
        manager.camera_client = None
        manager.camera_session = None
        manager.frames.clear()
        manager.camera_error = ""
        manager.camera_phase = "connecting"
    # Only an explicit Check Devices action reaches this code. The controller
    # must have verified USB cleanup and unchanged robot tasks. No device reset,
    # SSH, daemon restart or unconditional fault clearing happens here.
    with manager.new_camera_client() as fresh:
        status = fresh.status()
        if status.get("fault"):
            if (status.get("idle") is not True
                    or status.get("camera_hotplug_recovery") is not True
                    or status.get("recoverable_camera_fault") is not True):
                raise RuntimeError("控制器相机故障尚不能安全重连：" + str(status["fault"]))
            require_idle(manager)
            _require_not_cancelled(manager)
            fresh.recover_camera()
            status = fresh.status()
            if status.get("fault") or status.get("idle") is not True:
                raise RuntimeError("控制器未完成相机重连")
    # The caller starts a fresh local receiver after checking cancellation.
