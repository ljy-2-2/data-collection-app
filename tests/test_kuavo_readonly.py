import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from teleop_control.kuavo_readonly import ssh_command


def test_audited_readonly_stream_and_transport_with_fake_pipes():
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([sys.executable,str(root/'tests/fixtures/kuavo_readonly_fake.py')],cwd=root,
        env={**os.environ,'PYTHONPATH':str(root/'teleop_control/src')},capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr


def test_ssh_is_noninteractive_pinned_to_connection_host_and_config_is_local(tmp_path):
    options={'config':str(tmp_path/'connection.json')}
    command=ssh_command({'network_host':'192.0.2.1'},options)
    assert 'lab@192.0.2.1' in command
    assert 'BatchMode=yes' in command and 'StrictHostKeyChecking=yes' in command
    assert 'exec python3 -u -' in command[-1]
    assert '/home/yeap/桌面/Teleoperation' not in str(command)
    (tmp_path/'readonly.json').write_text(json.dumps({'user':'operator','ros_setup':'/opt/robot/setup.bash','control_path':'/tmp/robot-ssh'}))
    command=ssh_command({'network_host':'192.0.2.1'},options)
    assert 'operator@192.0.2.1' in command and command[command.index('-S')+1]=='/tmp/robot-ssh'
    (tmp_path/'readonly.json').write_text(json.dumps({'user':'-bad'}))
    with pytest.raises(ValueError):ssh_command({'network_host':'192.0.2.1'},options)
