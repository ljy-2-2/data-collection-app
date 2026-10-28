"""Keep the bundled teleop package isolated from Tianji in the test runner."""
import os
from pathlib import Path
import subprocess
import sys


def test_fake_ros_cleanup_scope():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / 'tests/fixtures/kuavo_cleanup_scope_fake.py')],
        cwd=root, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
        text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
