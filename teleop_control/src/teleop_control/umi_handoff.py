from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import httpx


class UmiHandoffError(RuntimeError):
    pass


class UmiControlHandoff:
    """Release UMI's PICO listener before the teleop XR service starts."""

    def __init__(
        self,
        compose_file: str | Path,
        *,
        monitor_url: str = "http://127.0.0.1:8770",
        stop_umi: Callable[[], None] | None = None,
        run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.compose_file = Path(compose_file).expanduser().resolve()
        self.monitor_url = monitor_url.rstrip("/")
        self.stop_umi = stop_umi
        self.run_command = run_command

    def prepare(self) -> None:
        try:
            response = httpx.get(f"{self.monitor_url}/api/state", timeout=1.0)
            if response.is_success and bool(response.json().get("recording")):
                raise UmiHandoffError("UMI 正在录制，请先停止并保存当前回合")
        except UmiHandoffError:
            raise
        except Exception:
            # An absent monitor is the normal state when UMI is not active.
            pass

        if self.stop_umi is not None:
            self.stop_umi()

        if not self.compose_file.is_file():
            raise UmiHandoffError(f"UMI compose 文件不存在: {self.compose_file}")
        try:
            result = self.run_command(
                [
                    "docker", "compose", "-f", str(self.compose_file),
                    "stop", "galaxis",
                ],
                capture_output=True,
                text=True,
                timeout=35.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UmiHandoffError(f"无法释放 UMI 控制端口: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise UmiHandoffError(f"无法停止 UMI 控制栈: {detail or result.returncode}")
