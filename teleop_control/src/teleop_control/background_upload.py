"""User-scoped upload service. UI sessions never own its process or credentials."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

from .platform_auth import PlatformTokenStore


def private_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        try:
            json.dump(payload, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, path)


def identity(destination, username):
    return hashlib.sha256(json.dumps([destination, username], sort_keys=True).encode()).hexdigest()


class UploadCredentials:
    """Upload-only session copies; never used to authenticate the desktop UI.

    No passwords are stored. A revoked/expired platform session still requires
    that operator to log in again; switching to another account cannot claim it.
    """
    def __init__(self, root):
        self.root = Path(root)

    def remember_session(self, destination, payload):
        user = payload['user']
        folder = self.root / 'auth' / identity(destination, user['username'])
        private_json(folder / 'owner.json', dict(destination=destination, username=user['username']))
        PlatformTokenStore(folder / 'session.json').write(token=payload['access_token'],
            expires_in=int(payload['expires_in']), user=user)

    def remember(self, destination, store):
        record = store.read()
        user = record.get('user') or {}
        username = user.get('username')
        if not username or not record.get('access_token'):
            return
        folder = self.root / 'auth' / identity(destination, username)
        path = folder / 'session.json'
        metadata = dict(destination=destination, username=username)
        try:
            if json.loads(path.read_text()) == record:
                return
        except (OSError, ValueError):
            pass
        private_json(folder / 'owner.json', metadata)
        private_json(path, record)

    def resolve(self, meta):
        destination, username = meta.get('upload_destination'), meta.get('author')
        if destination is None:
            # Pre-destination releases always used their configured default
            # platform. Never infer this from the current UI selection.
            try:
                destination = json.loads((self.root / 'config.json').read_text()).get('default_destination')
            except (OSError, ValueError):
                pass
        if not isinstance(destination, dict) or not isinstance(username, str) or not username:
            raise ValueError('回合缺少固定的平台或采集账号，请登录原账号核对后重试')
        folder = self.root / 'auth' / identity(destination, username)
        try:
            owner = json.loads((folder / 'owner.json').read_text())
        except (OSError, ValueError):
            raise ValueError('等待采集账号登录以恢复后台上传授权') from None
        if owner != dict(destination=destination, username=username):
            raise ValueError('后台上传授权与采集回合不一致')
        return destination, SimpleNamespace(store=PlatformTokenStore(folder / 'session.json'))


def uploader_spec(uploader):
    from .umi_uploader import UmiRosbagUploader
    if isinstance(uploader, UmiRosbagUploader):
        return dict(kind='umi', root=str(uploader.root.parent.resolve()))
    return dict(kind='teleop', root=str(uploader.output_root.resolve()),
                discover_completed=uploader.discover_completed)


def status_file(root, spec):
    key = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:24]
    return Path(root) / ('status-' + key + '.json')


def unit_quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('\n', '\\n') + '"'


def wait_for_worker(root, timeout=10):
    """Never present an operational app when its durable queue failed to start."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = json.loads((root / 'worker-state.json').read_text())
            os.kill(int(state['pid']), 0)
            if 0 <= time.time() - float(state['updated_at']) < 5:
                return
        except (OSError, ValueError, KeyError, TypeError):
            pass
        time.sleep(.1)
    raise RuntimeError('后台上传服务未就绪，已保留所有采集文件；请检查后台上传日志后重试')


def ensure_service(data_root, uploaders, *, systemd=True, default_destination=None):
    """Start one persistent queue per data root, independent of the app group."""
    root = Path(data_root).expanduser().resolve() / '.background-upload'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    config_path = root / 'config.json'
    specs = [uploader_spec(u) for u in uploaders]
    queue_specs = list(specs)
    try:
        previous = json.loads(config_path.read_text())
        # Release workspaces may change on upgrade. Keep scanning their queues
        # until the operator explicitly removes the old recording directories.
        for spec in previous.get('uploaders', []):
            if spec not in queue_specs:
                queue_specs.append(spec)
        default_destination = previous.get('default_destination') or default_destination
    except (OSError, ValueError):
        pass
    config = dict(version=1, uploaders=queue_specs, default_destination=default_destination)
    private_json(config_path, config)
    for uploader, spec in zip(uploaders, specs):
        uploader.external_status_path = status_file(root, spec)
    source = str(Path(__file__).resolve().parents[1])
    env = dict(os.environ, PYTHONPATH=source)
    env.pop('DATA_COLLECTION_GATE_FILE', None)
    command = [sys.executable, '-m', 'teleop_control.background_upload', '--config', str(config_path)]
    name = 'xiaokai-upload-' + hashlib.sha256(str(root).encode()).hexdigest()[:12] + '.service'
    if systemd:
        unit_dir = Path.home() / '.config/systemd/user'
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit_path = unit_dir / name
        unit = ('[Unit]\nDescription=Xiaokai completed recording upload queue\n'
                '[Service]\nType=simple\nExecStart=' + ' '.join(map(unit_quote, command)) + '\n'
                'Environment=' + unit_quote('PYTHONPATH=' + source) + '\n'
                'UnsetEnvironment=DATA_COLLECTION_GATE_FILE\nRestart=always\nRestartSec=5\n'
                'Nice=10\nTimeoutStopSec=600\nKillMode=mixed\n'
                '[Install]\nWantedBy=default.target\n')
        changed = not unit_path.exists() or unit_path.read_text() != unit
        if changed:
            unit_path.write_text(unit)
        try:
            already_active = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', name],
                                            capture_output=True, timeout=10).returncode == 0
            subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True,
                           capture_output=True, timeout=15)
            subprocess.run(['systemctl', '--user', 'enable', '--now', name], check=True,
                           capture_output=True, timeout=20)
            if changed and already_active:
                # A running old release finishes its current upload before
                # exiting; systemd then launches the newly configured version.
                subprocess.run(['systemctl', '--user', 'kill', '--kill-who=main',
                                '--signal=SIGUSR1', name], capture_output=True, timeout=10)
            wait_for_worker(root)
            return UploadCredentials(root)
        except (OSError, subprocess.SubprocessError):
            logging.getLogger(__name__).warning('用户服务暂不可用，启动独立后台上传进程')
    # Desktop sessions without systemd still keep uploads after window closure.
    # The worker's flock makes concurrent launches harmless.
    with (root / 'launch.log').open('ab') as output:
        subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=output,
                         stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    wait_for_worker(root)
    return UploadCredentials(root)


def run(config_path):
    from .umi_uploader import UmiRosbagUploader
    from .collection_uploader import CollectionUploader
    root = config_path.parent
    os.umask(0o077)
    os.environ.pop('DATA_COLLECTION_GATE_FILE', None)
    with (root / 'worker.lock').open('a') as singleton:
        try:
            fcntl.flock(singleton, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        handler = RotatingFileHandler(root / 'worker.log', maxBytes=2*1024*1024, backupCount=2)
        logging.basicConfig(level=logging.INFO, handlers=[handler],
                            format='%(asctime)s %(levelname)s %(name)s %(message)s')
        config = json.loads(config_path.read_text())
        credentials = UploadCredentials(root)
        uploaders = []
        for spec in config['uploaders']:
            if spec['kind'] == 'umi':
                uploader = UmiRosbagUploader(spec['root'], 'http://unused.invalid', targets=credentials)
            elif spec['kind'] == 'teleop':
                uploader = CollectionUploader(spec['root'], 'http://unused.invalid', targets=credentials,
                                              discover_completed=spec.get('discover_completed', False))
            else:
                raise ValueError('Unsupported background upload source')
            uploaders.append((spec, uploader))
        stopped = threading.Event()
        def stop(*_):
            stopped.set()
            for _, uploader in uploaders:
                uploader._stop.set()
                if hasattr(uploader, '_wake'):
                    uploader._wake.set()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1):
            signal.signal(sig, stop)
        for _, uploader in uploaders:
            uploader.start()
        try:
            while not stopped.wait(1):
                private_json(root / 'worker-state.json', dict(pid=os.getpid(), updated_at=time.time()))
                for spec, uploader in uploaders:
                    if hasattr(uploader, 'status'):
                        private_json(status_file(root, spec), uploader.status())
                if not config_path.exists() or json.loads(config_path.read_text()) != config:
                    stop()
        finally:
            stop()
            # Do not close an HTTP client under an in-flight transfer. Normal
            # app exit never reaches this path: the service remains running.
            while any(u._thread and u._thread.is_alive() for _, u in uploaders):
                private_json(root / 'worker-state.json', dict(pid=os.getpid(), updated_at=time.time()))
                time.sleep(1)
            for _, uploader in uploaders:
                if uploader._thread:
                    uploader._thread.join()
                uploader.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    run(args.config)


if __name__ == '__main__':
    main()
