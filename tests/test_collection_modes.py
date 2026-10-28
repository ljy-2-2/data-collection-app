"""Check the package capability contract and both UI entry guards together."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from collection_modes import default_collection_mode


@pytest.mark.parametrize('mode,expected', [
    (default_collection_mode('amd64'), ['tianji', 'kuavo-5w', 'agibot-g2']),
    (default_collection_mode('arm64'), ['tianji', 'kuavo-5w']),
    ('umi-tianji', ['tianji']),
    ('umi', []),
])
def test_mode_choices_and_backend_guards(tmp_path, mode, expected):
    # Isolate QCoreApplication lifetime from the render tests in this suite.
    script = '''
import json, sys
from PySide6.QtCore import QCoreApplication
from qt_ui.bridge import Bridge
app = QCoreApplication([])
mode, expected, config = json.loads(sys.argv[1])
bridge = Bridge({'APP_COLLECTION_MODE': mode}, demo=True, settings_path=config)
bridge.poll_timer.stop()
bridge.poll = lambda: None
assert bridge.robotKeys == expected
assert len(bridge.robotNames) == len(expected)
for robot in ('agibot-g2', 'tianji', 'kuavo-5w'):
    bridge.update(robot='tianji', teleopStep=1, error='')
    bridge.setValue('robot', robot)
    assert bridge.state['robot'] == (robot if robot in expected else 'tianji')
    bridge.configureTeleop('test', robot)
    assert bridge.state['teleopStep'] == (2 if robot in expected else 1)
    bridge.save_timer.stop()
bridge.shutdown()
'''
    import json
    result = subprocess.run([sys.executable, '-c', script, json.dumps([mode, expected, str(tmp_path)])],
        env={**os.environ, 'PYTHONPATH': os.pathsep.join((str(ROOT/'scripts'), str(ROOT/'teleop_control/src')))},
        cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
