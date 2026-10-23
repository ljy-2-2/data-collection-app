"""Route and EOF diagnostics: fake commands only, no sockets or devices."""
import json
import ssl
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from teleop_control.kuavo_network_diagnostics import inspect_route, tls_failure


@pytest.mark.parametrize('device,blocked',[('Mihomo',True),('clash0',True),('eno1',False),('tun0',False)])
def test_local_route_diagnoses_proxy_without_changing_network(monkeypatch,device,blocked):
    run=Mock(return_value=SimpleNamespace(returncode=0,stdout=json.dumps([{'dev':device,'gateway':'198.18.0.2','prefsrc':'198.18.0.1'}])))
    monkeypatch.setattr('subprocess.run',run)
    result=inspect_route('192.168.26.1')
    assert result['blocked'] is blocked
    assert result['interface']==device
    assert run.call_args.args[0]==['ip','-j','route','get','192.168.26.1']
    if blocked:assert '代理' in result['detail'] and '网线' in result['detail']


def test_route_tool_unavailable_does_not_prevent_supported_connections(monkeypatch):
    run=Mock(side_effect=FileNotFoundError('ip unavailable'));monkeypatch.setattr('subprocess.run',run)
    assert not inspect_route('192.168.26.1')['blocked']
    run.reset_mock()
    assert not inspect_route('192.168.26.1','kuavons')['blocked']
    run.assert_not_called()


def test_tls_eof_message_retains_evidence_not_credentials():
    message=tls_failure(ssl.SSLEOFError('UNEXPECTED_EOF_WHILE_READING'),
        {'network_host':'192.168.26.1','network_port':7443,'network_token_file':'secret-path'},'current',{'interface':'eno1'})
    assert '192.168.26.1:7443' in message and 'eno1' in message
    assert 'UNEXPECTED_EOF_WHILE_READING' in message
    assert 'secret-path' not in message and '未跳过证书校验' in message


@pytest.mark.parametrize('blocked',[True,False])
def test_worker_check_never_sends_commands_on_proxy_or_tls_failure(monkeypatch,blocked):
    import io
    import sys
    from types import ModuleType
    from teleop_control import kuavo_worker as worker
    client=Mock()
    client.get_status.side_effect=ssl.SSLEOFError('UNEXPECTED_EOF_WHILE_READING')
    fake=ModuleType('teleop.robots.kuavo5_service_client')
    fake.KuavoServiceClient=lambda config:client
    monkeypatch.setitem(sys.modules,fake.__name__,fake)
    monkeypatch.setattr(worker,'client_module',lambda:Mock())
    monkeypatch.setattr(worker,'connection',lambda _:dict(network_host='192.168.26.1',network_port=7443))
    monkeypatch.setattr('teleop_control.kuavo_network_diagnostics.inspect_route',
        lambda *args:dict(blocked=blocked,interface='Mihomo' if blocked else 'eno1',detail='代理网卡未直连'))
    monkeypatch.setattr('teleop_control.kuavo_vr.probe_subprocess',lambda:{'vr_checked':True,'vr_ready':False})
    output=io.StringIO()
    monkeypatch.setattr(sys,'stdout',output)
    monkeypatch.setattr(sys,'stdin',io.StringIO('{"config":"fake","network_namespace":"current"}\n'))
    monkeypatch.setattr(sys,'argv',['worker','check'])
    assert worker.main()==1
    rows=[json.loads(line[6:]) for line in output.getvalue().splitlines()]
    assert rows[0]['vr_checked'] is True
    assert rows[1]['network']['blocked'] is blocked
    assert ('代理网卡' if blocked else 'TLS 连接被对端提前关闭') in rows[-1]['error']
    assert client.get_status.call_count==int(not blocked)
    client.preparation.assert_not_called()
