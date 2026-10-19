import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'teleop_control/src'))
from teleop_control.upload_targets import UploadTargets
from teleop_control.platform_auth import PlatformLogin, PlatformTokenStore, PlatformBearerAuth
from teleop_control.collection_gate import CollectionGate
from teleop_control.umi_uploader import UmiRosbagUploader
from teleop_control import umi_uploader
from teleop_control.app import create_app, UploadTargetRequest
from teleop_control.main import MockController
from teleop_control.models import UmiStatus


def targets(tmp_path):
    login = PlatformLogin('https://cloud.test/api', PlatformTokenStore(tmp_path/'cloud-token.json'))
    t = UploadTargets(tmp_path/'data', login, 'https://cloud.test')
    for kind, item in t.logins.items():
        item.store.write(token=kind+'-token', expires_in=3600, user={'username':kind+'-operator'})
    return t


def test_selected_destination_survives_restart_and_has_separate_credentials(tmp_path):
    t=targets(tmp_path)
    cloud=t.logins['cloud']
    t.select('local')
    assert t.store.token()=='local-token'
    assert cloud.store.token()=='cloud-token'
    assert cloud.preferences.scope!=t.preferences.scope
    again=UploadTargets(tmp_path/'data',cloud,'https://cloud.test')
    assert again.selected=='local'
    assert again.store.path==t.store.path
    assert 'token' not in t.path.read_text()
    with pytest.raises(ValueError):t.select('unknown')


@pytest.mark.parametrize('saved', [[], None, {'kind': []}, {'kind': 'local', 'api_url': 'https://wrong.test'}])
def test_corrupt_saved_choice_cannot_break_startup_or_redirect_upload(tmp_path, saved):
    t = targets(tmp_path)
    t.path.parent.mkdir(parents=True, exist_ok=True)
    t.path.write_text(json.dumps(saved))
    again = UploadTargets(tmp_path/'data', t.logins['cloud'], 'https://cloud.test')
    assert again.selected == 'cloud'
    if saved is not None:
        with pytest.raises(ValueError):
            again.resolve({'upload_destination': saved})


def test_gate_and_completed_metadata_keep_selected_destination(tmp_path, monkeypatch):
    t=targets(tmp_path)
    def get(url, **kwargs):
        kind='local' if '127.0.0.1' in url else 'cloud'
        assert kwargs['headers']['Authorization']=='Bearer '+kind+'-token'
        return httpx.Response(200,json={'username':kind+'-operator'},request=httpx.Request('GET',url))
    monkeypatch.setattr(httpx,'get',get)
    g=CollectionGate(tmp_path/'gate.json',t)
    t.select('local');g.refresh()
    first=json.loads(g.path.read_text())
    assert first['available'] and first['username']=='local-operator'
    assert first['upload_destination']==t.targets['local']
    t.select('cloud');g.refresh()
    assert first['upload_destination']['kind']=='local'
    assert json.loads(g.path.read_text())['upload_destination']['kind']=='cloud'
    assert t.resolve({'upload_destination':first['upload_destination']})[1] is t.logins['local']
    assert t.resolve({})[1] is t.logins['cloud']
    with pytest.raises(ValueError):t.resolve({'upload_destination':{**first['upload_destination'],'api_url':'http://other.test'}})


def episode(root, name, target, owner):
    d=root/'rosbags/task'/name;d.mkdir(parents=True)
    (d/'meta.json').write_text(json.dumps({'author':owner,'upload_destination':target}))
    (d/'metadata.yaml').write_text('rosbag2_bagfile_information:\n  relative_file_paths: [round.mcap]\n')
    (d/'round.mcap').write_bytes(b'\x89MCAP0\r\npayload\x89MCAP0\r\n')
    return d


def test_uploads_follow_each_saved_target_even_after_switch_and_retry(tmp_path,monkeypatch):
    t=targets(tmp_path)
    local=episode(tmp_path,'local',t.targets['local'],'local-operator')
    cloud=episode(tmp_path,'cloud',t.targets['cloud'],'cloud-operator')
    requests=[];cloud_down=True
    def handle(r):
        requests.append(r)
        if r.url.host=='storage.test':
            assert 'Authorization' not in r.headers
            return httpx.Response(200)
        kind='local' if r.url.host=='127.0.0.1' else 'cloud'
        assert r.headers['Authorization']=='Bearer '+kind+'-token'
        if r.url.path.endswith('/uploads/presign'):
            if kind=='cloud' and cloud_down:raise httpx.ConnectError('cloud unavailable')
            data=json.loads(r.content)
            return httpx.Response(200,json={'urls':[{'filename':n,'key':kind+'/'+n,'put_url':'https://storage.test/'+kind+'/'+n} for n in data['filenames']]})
        return httpx.Response(200,json={})
    def client(*,timeout=None,store=None):
        if store is not None:
            assert timeout.connect == 5.0 and timeout.read == 30.0 and timeout.write == 30.0
        return httpx.Client(transport=httpx.MockTransport(handle),auth=PlatformBearerAuth(store or t.logins['cloud'].store))
    monkeypatch.setattr(umi_uploader,'platform_api_client',client)
    monkeypatch.setenv('DATA_COLLECTION_GATE_FILE',str(tmp_path/'offline-cloud-gate'))
    u=UmiRosbagUploader(tmp_path,t.targets['cloud']['api_url'],targets=t)
    try:
        t.select('local');u.scan_once()
        assert (local/'.data-platform-uploaded.json').exists()
        assert not (cloud/'.data-platform-uploaded.json').exists()
        t.select('cloud');cloud_down=False;u.scan_once()
        receipt=json.loads((cloud/'.data-platform-uploaded.json').read_text())
        assert receipt['upload_destination']['kind']=='cloud'
        assert json.loads((local/'.data-platform-uploaded.json').read_text())['upload_destination']['kind']=='local'
        registrations=[r for r in requests if r.method=='POST' and r.url.path.endswith('/episodes')]
        assert len(registrations)==2
        assert {json.loads(r.content)['author'] for r in registrations}=={'local-operator','cloud-operator'}
        count=len(requests);u.scan_once();assert len(requests)==count
        bad=episode(tmp_path,'unknown',{'kind':'local','api_url':'https://evil.test'},'local-operator')
        u.scan_once();assert len(requests)==count
        assert not (bad/'.data-platform-uploaded.json').exists()
    finally:u.stop()


def test_destination_switch_rejected_during_collection(tmp_path):
    t=targets(tmp_path)
    umi=SimpleNamespace(status=lambda:UmiStatus(state='recording'))
    app=create_app(MockController(),cors_origins=[],umi=umi,upload_targets=t,platform_login=t)
    route=next(r.endpoint for r in app.routes if r.path=='/platform/destinations' and 'POST' in r.methods)
    with pytest.raises(HTTPException) as e:route(UploadTargetRequest(kind='local'),SimpleNamespace(headers={}))
    assert e.value.status_code==409 and t.selected=='cloud'


def test_teleop_upload_queue_uses_recorded_platform_and_owner(tmp_path,monkeypatch):
    from teleop_control import collection_uploader as module
    t=targets(tmp_path);requests=[];cloud_down=True
    output=tmp_path/'episodes';output.mkdir()
    def make(name,kind,owner=None):
        d=output/name;d.mkdir()
        (d/'meta_info.json').write_text(json.dumps({'robot_type':'agibot_g2','cameras':{}}))
        (d/module.READY_MARKER).write_text(json.dumps({'author':owner or kind+'-operator',
            'import_task_id':'shared-session','upload_destination':t.targets[kind]}))
        return d
    local=make('local','local');cloud=make('cloud','cloud');other=make('other','local','different-user')
    def handle(r):
        requests.append(r)
        if r.url.host=='storage.test':
            assert 'Authorization' not in r.headers
            return httpx.Response(200)
        kind='local' if r.url.host=='127.0.0.1' else 'cloud'
        assert r.headers['Authorization']=='Bearer '+kind+'-token'
        if kind=='cloud' and cloud_down:raise httpx.ConnectError('cloud down')
        if r.url.path.endswith('/uploads/presign'):
            data=json.loads(r.content)
            return httpx.Response(200,json={'urls':[{'filename':n,'key':kind+'/'+n,'put_url':'https://storage.test/'+kind+'/'+n} for n in data['filenames']]})
        return httpx.Response(200,json={})
    def client(*,timeout=None,store=None):
        return httpx.Client(transport=httpx.MockTransport(handle),auth=PlatformBearerAuth(store or t.logins['cloud'].store))
    monkeypatch.setattr(module,'platform_api_client',client)
    monkeypatch.setenv('DATA_COLLECTION_GATE_FILE',str(tmp_path/'offline-gate'))
    u=module.CollectionUploader(output,t.targets['cloud']['api_url'],targets=t)
    try:
        t.select('cloud');u.scan_once()
        receipt=json.loads((local/module.UPLOADED_MARKER).read_text())
        assert receipt['upload_destination']==t.targets['local']
        assert not (cloud/module.UPLOADED_MARKER).exists()
        assert not (other/module.UPLOADED_MARKER).exists()
        t.select('local');cloud_down=False;u.scan_once()
        assert json.loads((cloud/module.UPLOADED_MARKER).read_text())['upload_destination']==t.targets['cloud']
        posts=[r for r in requests if r.method=='POST' and r.url.path.endswith('/episodes')]
        assert {json.loads(r.content)['author'] for r in posts}=={'local-operator','cloud-operator'}
        count=len(requests);u.scan_once();assert len(requests)==count
        bad=make('bad','local');p=json.loads((bad/module.READY_MARKER).read_text());p['upload_destination']['api_url']='https://unknown.test'
        (bad/module.READY_MARKER).write_text(json.dumps(p))
        u.scan_once();assert len(requests)==count and not (bad/module.UPLOADED_MARKER).exists()
    finally:u.stop()
