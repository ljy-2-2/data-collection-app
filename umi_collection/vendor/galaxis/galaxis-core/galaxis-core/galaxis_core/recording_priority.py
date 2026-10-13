"""Cross-container locks: capture takes priority over offline video encoding."""
import fcntl
import os
from pathlib import Path
import time


def open_lock(root, name):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / name, os.O_CREAT | os.O_RDWR, 0o666)
    try:
        os.fchmod(fd, 0o666)
    except PermissionError:
        pass
    return os.fdopen(fd, 'a+')


def try_lock(handle):
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


class CapturePriority:
    """Locks automatically release after a recorder crash; no stale flags."""
    def __init__(self, root):
        self.root = Path(root)
        self.intent = None
        self.resources = None

    def acquire(self, timeout=5):
        if self.intent is not None:
            return
        self.intent = open_lock(self.root, '.capture-intent.lock')
        self.resources = open_lock(self.root, '.video-processing.lock')
        try:
            deadline = time.monotonic() + timeout
            while not try_lock(self.intent):
                if time.monotonic() >= deadline:
                    raise RuntimeError('已有录制器正在使用此采集目录')
                time.sleep(.01)
            while not try_lock(self.resources):
                if time.monotonic() >= deadline:
                    raise RuntimeError('后台转码尚未释放采集资源，请稍后重试')
                time.sleep(.02)
        except BaseException:
            self.release()
            raise

    def release(self):
        for name in ('resources', 'intent'):
            handle = getattr(self, name)
            if handle is not None:
                handle.close()
                setattr(self, name, None)
