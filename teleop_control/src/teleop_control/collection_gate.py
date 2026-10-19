"""Keep network checks away from robot callbacks; gate only new recordings."""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import nullcontext
from pathlib import Path

from .platform_auth import PlatformLogin


def collection_context() -> dict:
    """Snapshot recording ownership; never infer it later from the active UI."""
    filename = os.environ.get("DATA_COLLECTION_GATE_FILE")
    if not filename:
        return {}
    try:
        value = json.loads(Path(filename).read_text())
        if value.get('available') is not True or not 0 <= time.time()-value['checked_at'] < 10:
            raise ValueError('采集平台登录状态未就绪')
        result = {'author': value['username']}
        if 'upload_destination' in value:
            result['upload_destination'] = value['upload_destination']
        return result
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RuntimeError('无法确认采集平台和账号，请检查所选平台登录状态') from exc


def collection_available(filename=None) -> bool:
    filename = filename or os.environ.get("DATA_COLLECTION_GATE_FILE")
    if not filename:
        return True  # Standalone tools outside the desktop supervisor.
    try:
        value = json.loads(Path(filename).read_text())
        return value.get("available") is True and 0 <= time.time() - value["checked_at"] < 10
    except (OSError, ValueError, TypeError, KeyError):
        return False


class CollectionGate:
    def __init__(self, path: Path, login: PlatformLogin):
        self.path, self.login = path, login
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="collection-platform-check", daemon=True)

    def _write(self, available: bool, username: str = "", destination: dict | None = None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        payload = dict(available=available, username=username, checked_at=time.time())
        if destination is not None:
            payload['upload_destination'] = destination
        temporary.write_text(json.dumps(payload))
        os.replace(temporary, self.path)

    def start(self, *, export_environment=True):
        self._write(False)
        if export_environment:
            os.environ["DATA_COLLECTION_GATE_FILE"] = str(self.path)
        self.thread.start()

    def _run(self):
        while not self.stopped.is_set():
            try:
                self.refresh()
            except Exception:
                self._write(False)
            self.stopped.wait(2)

    def refresh(self):
        # Selection and lease publication share a lock: an old network response
        # must never overwrite the newly selected destination's lease.
        lock = getattr(self.login, 'lock', None)
        with lock if lock is not None else nullcontext():
            status = self.login.status()
            destination = status.get('upload_destination')
            self._write(bool(status.get('authenticated')),
                        (status.get('user') or {}).get('username', ''),
                        destination if isinstance(destination, dict) else None)
            return status

    def stop(self):
        self.stopped.set()
        self.thread.join(timeout=6)
        self._write(False)
