"""Durable UMI destinations and separately scoped platform sessions."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from urllib.parse import urlsplit

from .platform_auth import PlatformLogin, PlatformTokenStore


def checked_url(value: str) -> str:
    value = value.strip().rstrip('/')
    u = urlsplit(value)
    if (u.scheme not in {'http', 'https'} or not u.hostname or u.username or u.password
            or u.query or u.fragment or any(c.isspace() for c in value)):
        raise ValueError('平台地址必须是不含账号密码的 HTTP(S) 地址')
    _ = u.port
    return value


class UploadTargets:
    def __init__(self, data_root: Path, default_login: PlatformLogin, web_url: str,
                 *, local_api='http://127.0.0.1:8000', local_web='http://127.0.0.1:5173'):
        self.lock = threading.RLock()
        self.background_credentials = None
        self.path = Path(data_root).expanduser() / '.umi-upload-target.json'
        self.default_kind = 'local' if urlsplit(default_login.api_url).hostname in {'localhost', '127.0.0.1', '::1'} else 'cloud'
        self.targets = {self.default_kind: dict(kind=self.default_kind,
            api_url=checked_url(default_login.api_url), web_url=checked_url(web_url))}
        self.logins = {self.default_kind: default_login}
        if self.default_kind != 'local':
            url = checked_url(local_api)
            key = hashlib.sha256(url.encode()).hexdigest()[:16]
            token_path = default_login.store.path.with_name(f'platform-local-{key}.json')
            self.targets['local'] = dict(kind='local', api_url=url, web_url=checked_url(local_web))
            self.logins['local'] = PlatformLogin(url, PlatformTokenStore(token_path))
        self.selected = self.default_kind
        try:
            previous = json.loads(self.path.read_text())
            if (isinstance(previous, dict) and isinstance(previous.get('kind'), str)
                    and previous.get('kind') in self.targets
                    and previous == self.targets[previous['kind']]):
                self.selected = previous['kind']
        except (OSError, ValueError, TypeError):
            pass

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.targets[self.selected])

    def public(self) -> dict:
        with self.lock:
            return dict(selected=self.selected, destinations=list(self.targets.values()),
                        default_kind=self.default_kind, **self.snapshot())

    def select(self, kind: str) -> dict:
        with self.lock:
            if kind not in self.targets:
                raise ValueError('未配置此上传平台')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.targets[kind]))
            os.replace(tmp, self.path)
            self.selected = kind
            return self.public()

    @property
    def api_url(self):
        return self.snapshot()['api_url']

    @property
    def store(self):
        with self.lock:
            return self.logins[self.selected].store

    @property
    def preferences(self):
        with self.lock:
            return self.logins[self.selected].preferences

    def status(self):
        with self.lock:
            result = self.logins[self.selected].status()
            return {**result, 'upload_destination': self.snapshot()}

    def login(self, *args, **kwargs):
        with self.lock:
            if self.background_credentials is not None:
                destination = self.snapshot()
                self.logins[self.selected].background_session = lambda payload: self.background_credentials.remember_session(destination, payload)
            result = self.logins[self.selected].login(*args, **kwargs)
            return {**result,
                    'upload_destination': self.snapshot()}

    def logout(self):
        with self.lock:
            return self.logins[self.selected].logout()

    def resolve(self, meta: dict):
        """Never reinterpret a saved destination using the current selection."""
        target = meta.get('upload_destination')
        if target is None:
            return self.targets[self.default_kind], self.logins[self.default_kind]
        if not isinstance(target, dict):
            raise ValueError('采集回合上传目标无效，保留本地文件')
        kind = target.get('kind')
        if not isinstance(kind, str) or kind not in self.targets or target != self.targets[kind]:
            raise ValueError('采集回合平台地址与当前配置不一致，保留文件等待恢复原平台配置')
        return self.targets[kind], self.logins[kind]
