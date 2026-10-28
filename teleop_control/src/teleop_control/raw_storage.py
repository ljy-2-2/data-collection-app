"""Explicit local recording session, independent of any platform or account."""
import getpass
import os
import threading
from pathlib import Path


def raw_only_enabled():
    return os.environ.get('DATA_COLLECTION_STORAGE_MODE', 'platform') == 'raw-only'


class RawOnlySession:
    raw_only = True
    api_url = ''

    def __init__(self):
        self.lock = threading.RLock()
        self.username = getpass.getuser()
        self.preferences = self
        self.store = self

    def user(self):
        return {'username': self.username}

    def status(self):
        # authenticated is the desktop's access gate, not a remote identity.
        return dict(authenticated=True, user=self.user(), session_type='local',
                    storage_mode='raw-only', upload_enabled=False)

    def snapshot(self):
        return None

    def public(self):
        return dict(selected='raw-only', destinations=[], storage_mode='raw-only',
                    auto_login=False, has_saved_password=False)

    def select(self, kind):
        raise ValueError('本设备只保存原始数据，已禁用平台上传')


def retry_raw_finalization(data_root):
    """Retry durable finalizer jobs without constructing an upload client."""
    from .umi_queue import read_json
    root = Path(data_root) / 'rosbags'
    for failure in root.glob('*/*/recording-failure.json'):
        stage = read_json(failure).get('stage')
        if stage == 'recovery' or (stage == 'mp4' and (failure.parent / '.mp4-pending.json').is_file()):
            failure.unlink(missing_ok=True)
