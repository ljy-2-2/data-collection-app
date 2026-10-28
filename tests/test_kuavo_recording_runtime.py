import os
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.parametrize('fixture',['kuavo_recording_runtime_fake.py','kuavo_wire_recording_regression.py'])
def test_fake_recording_runtime(fixture):
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([sys.executable,str(root/'tests/fixtures'/fixture)],
        cwd=root,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
