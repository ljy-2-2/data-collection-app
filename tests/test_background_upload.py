import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'teleop_control/src'), str(ROOT / 'scripts')]
from teleop_control.background_upload import UploadCredentials, private_json, ensure_service
from teleop_control.platform_auth import PlatformTokenStore, PlatformBearerAuth
from teleop_control.umi_uploader import UmiRosbagUploader
from run_app import clear_previous_app_login


def remember(root, destination, username='operator'):
    store = PlatformTokenStore(root / ('ui-' + username + '.json'))
    store.write(token='test-only-' + username, expires_in=3600, user={'username': username})
    credentials = UploadCredentials(root / '.background-upload')
    credentials.remember(destination, store)
    return store, credentials


def episode(root, name, destination, author='operator'):
    folder = root / 'rosbags' / 'background-test' / name
    folder.mkdir(parents=True)
    (folder / 'sample.mcap').write_bytes(b'\x89MCAP0\r\nframe-data\x89MCAP0\r\n')
    (folder / 'metadata.yaml').write_text('rosbag2_bagfile_information:\n  relative_file_paths: [sample.mcap]\n')
    private_json(folder / 'meta.json', dict(author=author, upload_destination=destination))
    return folder


def wait_for(predicate, seconds=12):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('background upload did not progress')
        time.sleep(.05)


def test_background_credentials_survive_ui_close_without_authenticating_ui(tmp_path):
    target = dict(kind='local', api_url='http://127.0.0.1:8000', web_url='http://127.0.0.1:5173')
    store, credentials = remember(tmp_path, target)
    clear_previous_app_login({'PLATFORM_TOKEN_FILE': str(store.path)})
    assert not store.token()
    dest, login = credentials.resolve(dict(author='operator', upload_destination=target))
    assert dest == target and login.store.token() == 'test-only-operator'
    assert stat.S_IMODE(login.store.path.stat().st_mode) == 0o600
    assert 'password' not in login.store.read()
    with pytest.raises(ValueError):
        credentials.resolve(dict(author='someone-else', upload_destination=target))
    with pytest.raises(ValueError):
        credentials.resolve(dict(author='operator', upload_destination={**target, 'api_url':'http://other.invalid'}))
    with pytest.raises(ValueError):
        credentials.resolve(dict(author='operator'))


def test_same_platform_multiple_accounts_never_reuse_another_token(tmp_path, monkeypatch):
    from teleop_control import umi_uploader
    target = dict(kind='local', api_url='http://platform.test', web_url='http://platform.test')
    _, credentials = remember(tmp_path, target, 'a')
    remember(tmp_path, target, 'b')
    one = episode(tmp_path, '001', target, 'a')
    two = episode(tmp_path, '002', target, 'b')
    owners = []
    def handler(request):
        if request.method == 'POST' and request.url.path == '/uploads/presign':
            names = json.loads(request.content)['filenames']
            return httpx.Response(200, json={'urls':[dict(filename=n, key='raw/'+n, put_url='http://store.test/'+n) for n in names]})
        if request.method == 'POST' and request.url.path == '/episodes':
            owner = json.loads(request.content)['author']
            assert request.headers['Authorization'] == 'Bearer test-only-' + owner
            owners.append(owner)
        return httpx.Response(200, json={})
    monkeypatch.setattr(umi_uploader, 'platform_api_client', lambda timeout=None, store=None:
                        httpx.Client(transport=httpx.MockTransport(handler), auth=PlatformBearerAuth(store or PlatformTokenStore(tmp_path/'none'))))
    uploader = UmiRosbagUploader(tmp_path, target['api_url'], targets=credentials)
    try:
        uploader.scan_once()
        assert owners == ['a', 'b']
        assert all((p / '.data-platform-uploaded.json').exists() for p in [one,two])
    finally:
        uploader.stop()


def test_explicit_ui_logout_revokes_only_ui_session(tmp_path, monkeypatch):
    from teleop_control.platform_auth import PlatformLogin
    from teleop_control.upload_targets import UploadTargets
    issued, revoked = [], []
    def post(url, **kwargs):
        if url.endswith('/auth/token'):
            token = 'test-session-' + str(len(issued))
            issued.append(token)
            return httpx.Response(200, json=dict(access_token=token, expires_in=3600,
                user=dict(username='operator')), request=httpx.Request('POST',url))
        revoked.append(kwargs['headers']['Authorization'])
        return httpx.Response(200, json={})
    monkeypatch.setattr(httpx, 'post', post)
    login = PlatformLogin('http://127.0.0.1:8000', PlatformTokenStore(tmp_path/'ui.json'))
    targets = UploadTargets(tmp_path/'data', login, 'http://127.0.0.1:5173')
    targets.background_credentials = UploadCredentials(tmp_path/'.background-upload')
    targets.login('operator', 'not-a-real-password')
    _, background = targets.background_credentials.resolve(dict(author='operator', upload_destination=targets.snapshot()))
    assert login.store.token() == issued[0]
    assert background.store.token() == issued[1]
    targets.logout()
    assert not login.store.token()
    assert revoked == ['Bearer '+issued[0]]
    assert background.store.token() == issued[1]


def test_detached_worker_uploads_after_launcher_exit_and_restart(tmp_path):
    """Real processes and HTTP: close UI, retry a failed PUT, then restart worker."""
    uploaded, registered, failed_once = {}, [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def handle_request(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            if self.path.startswith('/objects/'):
                assert 'Authorization' not in self.headers
                if not failed_once:
                    failed_once.append(True)
                    self.send_response(503); self.end_headers()
                    return
                uploaded[self.path] = body
                result = {}
            else:
                assert self.headers.get('Authorization') == 'Bearer test-only-operator'
                data = json.loads(body)
                if self.path == '/uploads/presign':
                    result = {'urls':[dict(filename=n, key='raw/'+n,
                        put_url=base+'/objects/'+n) for n in data['filenames']]}
                elif self.path == '/episodes':
                    registered.append(data)
                    result = {'id':data['id']}
                else: result = {}
            payload = json.dumps(result).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(payload)))
            self.end_headers(); self.wfile.write(payload)
        do_POST = handle_request
        do_PUT = handle_request
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    base = 'http://127.0.0.1:' + str(server.server_port)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    target = dict(kind='local', api_url=base, web_url=base)
    store, credentials = remember(tmp_path, target)
    first = episode(tmp_path, '001', target)
    (first / '.mp4-pending.json').write_text('{}')
    config_path = credentials.root / 'config.json'
    private_json(config_path, dict(version=1, uploaders=[dict(kind='umi', root=str(tmp_path))]))
    env = dict(os.environ, PYTHONPATH=str(ROOT / 'teleop_control/src'))
    env['DATA_COLLECTION_GATE_FILE'] = str(tmp_path/'closed-ui-gate')
    command = [sys.executable, '-m', 'teleop_control.background_upload', '--config', str(config_path)]
    launcher = ('import subprocess,sys; p=subprocess.Popen(' + repr(command) +
                ',start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(p.pid)')
    worker_pid = None
    second_proc = None
    try:
        # The launcher exits completely; it cannot keep the upload thread alive.
        worker_pid = int(subprocess.check_output([sys.executable, '-c', launcher], env=env, text=True))
        wait_for(lambda: (credentials.root/'worker-state.json').exists())
        clear_previous_app_login({'PLATFORM_TOKEN_FILE':str(store.path)})
        assert not store.token() and not registered
        (first / '.mp4-pending.json').unlink()
        wait_for(lambda: (first/'.data-platform-uploaded.json').exists())
        assert uploaded['/objects/sample.mcap'] == (first/'sample.mcap').read_bytes()
        assert len(registered) == 1
        # A second app launch must not create a competing uploader.
        assert subprocess.run(command, env=env, timeout=5).returncode == 0
        os.kill(worker_pid, signal.SIGKILL)
        time.sleep(.3)
        second = episode(tmp_path, '002', target)
        second_proc = subprocess.Popen(command, env=env)
        wait_for(lambda: (second/'.data-platform-uploaded.json').exists())
        assert len(registered) == 2 and registered[1]['author'] == 'operator'
    finally:
        if worker_pid:
            try: os.kill(worker_pid, signal.SIGTERM)
            except ProcessLookupError: pass
        if second_proc:
            second_proc.terminate(); second_proc.wait(8)
        server.shutdown(); server.server_close(); thread.join(3)


def test_systemd_service_is_independent_and_does_not_signal_first_start(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from teleop_control import background_upload
    monkeypatch.setenv('HOME', str(tmp_path))
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=3 if 'is-active' in command else 0)
    monkeypatch.setattr(background_upload.subprocess, 'run', run)
    monkeypatch.setattr(background_upload, 'wait_for_worker', lambda root: None)
    uploader = UmiRosbagUploader(tmp_path/'data', 'http://platform.test')
    try:
        previous = dict(kind='teleop', root=str(tmp_path/'previous-release'), discover_completed=False)
        private_json(tmp_path/'data/.background-upload/config.json', dict(uploaders=[previous]))
        ensure_service(tmp_path/'data', [uploader])
        config=json.loads((tmp_path/'data/.background-upload/config.json').read_text())
        assert previous in config['uploaders']
        unit = next((tmp_path/'.config/systemd/user').glob('xiaokai-upload-*.service')).read_text()
        assert 'Restart=always' in unit and 'Nice=10' in unit
        assert 'PartOf=' not in unit and 'BindsTo=' not in unit
        assert not any('kill' in cmd for cmd in calls)
    finally: uploader.stop()


def test_stale_or_missing_worker_cannot_silently_disable_uploads(tmp_path):
    from teleop_control.background_upload import wait_for_worker
    private_json(tmp_path/'worker-state.json', dict(pid=os.getpid(), updated_at=time.time()-60))
    with pytest.raises(RuntimeError, match='后台上传服务未就绪'):
        wait_for_worker(tmp_path, timeout=.15)


@pytest.mark.parametrize('metadata', [{}, {'author': 'old-user'}, None])
def test_completed_teleop_never_requires_old_authorization(tmp_path, monkeypatch, metadata):
    from teleop_control import collection_uploader
    folder = tmp_path / 'completed'
    folder.mkdir()
    (folder / collection_uploader.READY_MARKER).write_text(json.dumps(metadata) if metadata is not None else '{broken')
    receipt = folder / collection_uploader.UPLOADED_MARKER
    receipt.write_text('{"episode_id":"already-uploaded"}')
    before = receipt.read_bytes()
    class NoAuthorization:
        def resolve(self, info):
            raise AssertionError('Uploaded data must not resolve credentials')
    def no_network(request):
        raise AssertionError('Uploaded data must not contact platform')
    monkeypatch.setattr(collection_uploader, 'collection_available', lambda: True)
    with httpx.Client(transport=httpx.MockTransport(no_network)) as client:
        uploader = collection_uploader.CollectionUploader(tmp_path, 'http://platform.test', targets=NoAuthorization(), client=client)
        uploader._set_status(folder, 'failed', '等待采集账号登录以恢复后台上传授权')
        uploader.scan_once()
        assert uploader.status() == dict(pending=0, uploaded=1, uploading=0, failed=0, error='')
        assert receipt.read_bytes() == before


def test_completed_teleop_still_counts_toward_pending_batch(tmp_path, monkeypatch):
    from teleop_control import collection_uploader
    info = dict(author='operator', import_task_id='same-batch')
    for name in ('completed', 'pending'):
        folder = tmp_path / name
        folder.mkdir()
        (folder / collection_uploader.READY_MARKER).write_text(json.dumps(info))
    (tmp_path / 'completed' / collection_uploader.UPLOADED_MARKER).write_text('{}')
    totals = []
    uploaded = []
    with httpx.Client() as client:
        uploader = collection_uploader.CollectionUploader(tmp_path, 'http://platform.test', client=client)
        monkeypatch.setattr(uploader, '_select_destination', lambda info: True)
        monkeypatch.setattr(uploader, '_sync_batch', lambda info, state, **kwargs: totals.append(kwargs['episode_total']))
        monkeypatch.setattr(uploader, '_upload_episode', lambda folder, info: uploaded.append(folder.name))
        uploader.scan_once()
    assert uploaded == ['pending']
    assert totals == [2, 2, 2]


def test_unuploaded_teleop_still_requires_original_authorization(tmp_path):
    from teleop_control import collection_uploader
    folder = tmp_path / 'pending'
    folder.mkdir()
    (folder / collection_uploader.READY_MARKER).write_text(json.dumps(dict(author='old-user')))
    with httpx.Client() as client:
        uploader = collection_uploader.CollectionUploader(tmp_path, 'http://platform.test',
            targets=UploadCredentials(tmp_path / 'missing-auth'), client=client)
        uploader.scan_once()
        assert uploader.status()['failed'] == 1
        assert not (folder / collection_uploader.UPLOADED_MARKER).exists()


def test_worker_upgrade_waits_for_new_implementation(tmp_path, monkeypatch):
    from teleop_control import background_upload
    root=tmp_path/'.background-upload'
    config=root/'config.json'
    private_json(config,dict(version=1,uploaders=[],implementation='old-test-version'))
    command=[sys.executable,'-m','teleop_control.background_upload','--config',str(config)]
    env=dict(os.environ,PYTHONPATH=str(ROOT/'teleop_control/src'))
    original_popen=subprocess.Popen
    old=original_popen(command,env=env)
    replacements=[]
    def launch(*args,**kwargs):
        process=original_popen(*args,**kwargs)
        replacements.append(process)
        return process
    try:
        wait_for(lambda:(root/'worker-state.json').exists())
        monkeypatch.setattr(background_upload.subprocess,'Popen',launch)
        background_upload.ensure_service(tmp_path,[],systemd=False)
        state=json.loads((root/'worker-state.json').read_text())
        expected=json.loads(config.read_text())['implementation']
        assert state['implementation']==expected and expected!='old-test-version'
        assert state['pid']==replacements[0].pid and state['pid']!=old.pid
        assert old.wait(5)==0
        duplicate=original_popen(command,env=env)
        assert duplicate.wait(5)==0
    finally:
        for process in [old,*replacements]:
            if process.poll() is None:process.terminate()
            process.wait(8)


def test_old_worker_heartbeat_is_not_ready_after_upgrade(tmp_path):
    from teleop_control.background_upload import wait_for_worker
    private_json(tmp_path/'config.json',dict(implementation='new-version'))
    private_json(tmp_path/'worker-state.json',dict(pid=os.getpid(),updated_at=time.time(),implementation='old-version'))
    with pytest.raises(RuntimeError,match='未就绪'):
        wait_for_worker(tmp_path,timeout=.15)
