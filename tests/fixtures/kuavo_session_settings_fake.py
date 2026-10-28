"""Exercise the bundled SDK with fake clients; no sockets or hardware allowed."""
import copy
import hashlib
import json
from pathlib import Path
import socket
from types import SimpleNamespace
from unittest.mock import Mock

def forbidden(*args, **kwargs):
    raise AssertionError('offline test attempted network access')
socket.socket.connect = forbidden
socket.create_connection = forbidden

from teleop_control.kuavo_worker import client_module, VENDOR
module = client_module()
assert module.ROOT == VENDOR
assert module.host_module().ROOT == VENDOR
from teleop_control.kuavo_session_settings import settings_form, settings_operation, require_selected_contract

directory = Path(__file__).parent/'kuavo_settings'
snapshot = json.loads((directory/'snapshot.json').read_text())
schema = json.loads((directory/'schema.json').read_text())
raw = dict(ok=True, editable=True, locked_reason=None, snapshot=snapshot,
           schema=schema, defaults=copy.deepcopy(snapshot['values']), service_instance_id='a'*32,
           installation_sha256=snapshot['installation_sha256'])
form = settings_form(raw)
assert len(form['values']) == 14 and form['schema']['left_arm_scale']['max'] == 2
identity = dict(expected_revision=form['revision'], expected_service_instance_id=form['service_instance_id'],
                expected_installation_sha256=form['installation_sha256'])
changes = {'left_arm_scale':.5, 'right_arm_speed_rad_s':.6}
updated = copy.deepcopy(raw)
updated['snapshot']['values'].update(changes)
updated['snapshot']['revision'] += 1
updated['snapshot']['settings_sha256'] = hashlib.sha256(json.dumps(
    {k:v for k,v in updated['snapshot'].items() if k!='settings_sha256'},sort_keys=True,separators=(',', ':'),allow_nan=False).encode()).hexdigest()
client = SimpleNamespace(get_status=Mock(return_value={'management_ready':True,'service':{
    'service_instance_id':form['service_instance_id'],'installation_sha256':form['installation_sha256']}}),
    get_session_settings=Mock(return_value=raw),
    preview_session_settings=Mock(return_value=updated),
    apply_session_settings=Mock(return_value=updated),reset_session_settings=Mock(return_value=updated))
assert settings_operation(client,{'action':'read'})['session_settings']==form
client.apply_session_settings.assert_not_called()
request=dict(action='preview',changes=changes,**identity)
assert settings_operation(client,request)['settings_preview']['ok']
client.apply_session_settings.assert_not_called()
client.get_session_settings.return_value=updated
result=settings_operation(client,{**request,'action':'apply','confirm':True})
assert result['session_settings']['values']['left_arm_scale']==.5
assert result['session_settings']['values']['right_arm_scale']==1
client.apply_session_settings.assert_called_once_with(changes,confirm=True,**identity)
assert client.preview_session_settings.call_args.kwargs==identity  # Original revision, not preview's revision.

client.apply_session_settings.reset_mock()
client.preview_session_settings.return_value={'ok':False,'error':'revision conflict'}
try:settings_operation(client,{**request,'action':'apply','confirm':True})
except ValueError:pass
else:raise AssertionError('failed preview applied')
client.apply_session_settings.assert_not_called()
client.preview_session_settings.return_value=updated
client.apply_session_settings.side_effect=TimeoutError('outcome unknown')
try:settings_operation(client,{**request,'action':'apply','confirm':True})
except TimeoutError:pass
else:raise AssertionError('lost reply accepted')
assert client.apply_session_settings.call_count==1
client.apply_session_settings.side_effect=None
client.get_session_settings.return_value=raw
try:settings_operation(client,{**request,'action':'apply','confirm':True})
except ValueError:pass
else:raise AssertionError('mismatched readback accepted')

client.get_session_settings.return_value=raw
client.reset_session_settings.return_value=raw
settings_operation(client,dict(action='reset',confirm=True,**identity))
client.reset_session_settings.assert_called_once_with(confirm=True,**identity)
tampered=copy.deepcopy(raw);tampered['snapshot']['values']['left_arm_scale']=.3
try:settings_form(tampered)
except ValueError:pass
else:raise AssertionError('tampered snapshot accepted')

expected={k:form[k] for k in ('revision','settings_sha256','service_instance_id','installation_sha256')}
contract=dict(service_instance_id=form['service_instance_id'],installation_sha256=form['installation_sha256'],session_settings=snapshot)
require_selected_contract(contract,{'expected_settings':expected})
try:require_selected_contract(contract,{})
except ValueError:pass
else:raise AssertionError('new service started with old local scaling')
for changed in [dict(service_instance_id='b'*32),dict(session_settings=updated['snapshot'])]:
    try:require_selected_contract({**contract,**changed},{'expected_settings':expected})
    except ValueError:pass
    else:raise AssertionError('base control or stale settings accepted')

from teleop_control.kuavo_worker import execute
from unittest.mock import patch
import tempfile
with tempfile.TemporaryDirectory() as tmp:
    fake=SimpleNamespace(select_session=lambda *a:dict(capabilities=[]),
                         host_module=Mock(side_effect=AssertionError('host must not start')))
    events=[]
    with patch('signal.signal'),patch('threading.Thread.start'):
        code=execute(fake,{},dict(network_namespace='current',output=tmp+'/episode',task='test',arm_mode='dual_base'),events.append)
    assert code==1 and '不支持双臂＋底盘' in events[-1]['session']['error']
    assert not Path(tmp,'episode').exists()
    fake.host_module.assert_not_called()
print('Portable SDK: 14 fields, preview/apply, revision, readback, no retry, reset, integrity and arms-only guards passed')

for mode in ('dual','left','right','dual_base'):
    require_selected_contract({**contract,'capabilities':['base_vendor_vr_x_v1']}, {'arm_mode':mode,'expected_settings':expected})
# A service claiming success with a self-consistent but unmodified snapshot is rejected.
client.preview_session_settings.return_value=raw
client.apply_session_settings.return_value=raw
client.get_session_settings.return_value=raw
try:settings_operation(client,{**request,'action':'apply','confirm':True})
except ValueError as exc:assert '回读值' in str(exc)
else:raise AssertionError('ignored changes accepted as applied')
