from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .devices import port_open
from .gdk_env import add_xr_runtime_env


class XrServiceStartError(RuntimeError):
    pass


class XrServiceLauncher:
    def __init__(
        self,
        script: str,
        *,
        port_checker: Callable[[str, int, float], bool] = port_open,
        process_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        timeout_seconds: float = 20.0,
        log_path: Path = Path("/tmp/data-platform-xr-service.log"),
    ) -> None:
        self.script = Path(script).expanduser()
        self.port_checker = port_checker
        self.process_factory = process_factory
        self.timeout_seconds = timeout_seconds
        self.log_path = log_path
        self._lock = threading.Lock()

    def _service_pids(self) -> list[int]:
        executable = (self.script.parent / "RoboticsServiceProcess").resolve()
        matches: list[int] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if (entry / "exe").resolve() == executable:
                    matches.append(int(entry.name))
            except (OSError, RuntimeError):
                continue
        return matches

    def _ready(self) -> bool:
        return self.port_checker("127.0.0.1", 60061, 0.3)

    def _start_locked(self) -> bool:
        if self._ready():
            return False
        if not self.script.is_file():
            raise XrServiceStartError(f"Galaxis VR launcher not found: {self.script}")

        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as log:
                process = self.process_factory(
                    ["bash", str(self.script)],
                    cwd=str(self.script.parent),
                    env=add_xr_runtime_env(os.environ.copy(), str(self.script)),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as exc:
            raise XrServiceStartError(f"Cannot start Galaxis VR: {exc}") from exc

        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if self._ready():
                return True
            return_code = process.poll()
            # The official ARM launcher backgrounds RoboticsServiceProcess and
            # exits successfully before its socket is ready. Readiness belongs
            # to the service, not to the short-lived shell wrapper.
            if return_code is not None and return_code != 0:
                raise XrServiceStartError(
                    f"Galaxis VR exited with code {return_code}; log: {self.log_path}"
                )
            time.sleep(0.5)
        raise XrServiceStartError(
            f"Galaxis VR did not open port 60061 within {self.timeout_seconds:g}s; "
            f"log: {self.log_path}"
        )

    def ensure_started(self) -> bool:
        """Start Galaxis VR when needed. Return True only when a process was started."""
        with self._lock:
            return self._start_locked()

    def restart(self) -> None:
        """Replace the exact vendor bridge process after a connected-but-dead stream."""
        with self._lock:
            pids = self._service_pids()
            if self._ready() and not pids:
                raise XrServiceStartError(
                    "Port 60061 is open but its owner is not RoboticsServiceProcess"
                )
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 5.0
            while self._ready() and time.monotonic() < deadline:
                time.sleep(0.1)
            if self._ready():
                for pid in self._service_pids():
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                deadline = time.monotonic() + 2.0
                while self._ready() and time.monotonic() < deadline:
                    time.sleep(0.1)
            if self._ready():
                raise XrServiceStartError("旧 Galaxis VR 服务无法停止")
            self._start_locked()
