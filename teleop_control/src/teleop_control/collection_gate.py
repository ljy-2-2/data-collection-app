"""Keep network checks away from robot callbacks; gate only new recordings."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .platform_auth import PlatformLogin


def collection_available() -> bool:
    filename = os.environ.get("DATA_COLLECTION_GATE_FILE")
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

    def _write(self, available: bool, username: str = ""):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(dict(available=available, username=username, checked_at=time.time())))
        os.replace(temporary, self.path)

    def start(self):
        self._write(False)
        os.environ["DATA_COLLECTION_GATE_FILE"] = str(self.path)
        self.thread.start()

    def _run(self):
        while not self.stopped.is_set():
            try:
                status = self.login.status()
                self._write(bool(status.get("authenticated")), (status.get("user") or {}).get("username", ""))
            except Exception:
                self._write(False)
            self.stopped.wait(2)

    def stop(self):
        self.stopped.set()
        self.thread.join(timeout=6)
        self._write(False)
