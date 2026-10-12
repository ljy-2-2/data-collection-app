"""Idle-only cleanup of the App's local preview receiver. No remote restart."""


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
    # The caller starts a fresh local receiver after checking cancellation.
