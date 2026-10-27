"""Check and rebuild the local Galaxis data path while robot control is idle."""
from pathlib import Path
import threading
import time

from .xr_service import XrServiceLauncher


class LocalVRConnection:
    def __init__(self, config, *, input_factory, ensure_service, service=None, cancelled=None):
        self.config = config
        self.input_factory = input_factory
        self.ensure_service = ensure_service
        self.service = service or XrServiceLauncher(
            str(Path(config["service_dir"]) / "runService.sh"), timeout_seconds=8)
        self.cancelled = cancelled if cancelled is not None else threading.Event()
        self.input = None
        self.error = "等待新的 VR 时间戳"

    def snapshot(self):
        if self.input is None:
            return None, False, self.error, False
        frame, emergency, error, connected = self.input.snapshot()
        return frame, emergency, "" if connected else (self.error or error), connected

    def _not_cancelled(self):
        if self.cancelled.is_set():
            raise RuntimeError("本机 VR 连接检查已取消")

    def _wait_ready(self):
        deadline = time.monotonic() + 2.5
        while True:
            self._not_cancelled()
            if self.input.snapshot()[3]:
                self.error = ""
                return True
            if time.monotonic() >= deadline:
                return False
            self.cancelled.wait(0.05)

    def check(self):
        self._not_cancelled()
        if self.input is not None and self.input.snapshot()[3]:
            self.error = ""
            return
        try:
            if self.input is None:
                self.ensure_service(self.config)
                self._not_cancelled()
                self.input = self.input_factory(self.config["vr_timeout_s"])
                if self._wait_ready():
                    return
            # Discard cached SDK data and reconnect to one fresh local bridge.
            self.input.close()
            self.input = None
            self._not_cancelled()
            self.service.restart()
            self._not_cancelled()
            self.input = self.input_factory(self.config["vr_timeout_s"])
            if not self._wait_ready():
                raise RuntimeError(
                    "已重启本机 Galaxis VR，但仍未收到时间戳持续更新的数据；"
                    "请确认 PICO 处于 WORK，且 Head、Controllers、Send Data 已开启")
        except Exception as exc:
            self.error = str(exc)
            raise

    def close(self):
        try:
            if self.input is not None:
                self.input.close()
                self.input = None
        finally:
            # Closing the SDK subscriber alone leaves the PICO sender connected
            # to RoboticsService. Stop that exact bridge to close its sockets.
            self.service.stop()
