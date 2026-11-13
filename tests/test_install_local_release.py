import fcntl
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from install_local_release import install


@pytest.fixture
def release(tmp_path):
    data = tmp_path / 'data'
    current = data / 'versions/1.old'
    old = data / 'workspaces/1.old'
    package = tmp_path / 'new'
    config = tmp_path / 'config'
    config.mkdir()
    (config / '.env').write_text('preserved account settings')
    for root, version in ((current, '1.old'), (package, '2.new')):
        (root / 'app/scripts').mkdir(parents=True)
        (root / 'app/scripts/main.py').write_text(version)
        (root / 'package-build.json').write_text(json.dumps({'build_id': version}))
    (package / 'app-sha256.json').write_text(json.dumps({
        'scripts/main.py': hashlib.sha256(b'2.new').hexdigest()}))
    (old / 'scripts').mkdir(parents=True)
    (old / 'scripts/main.py').write_text('1.old')
    (old / 'teleop_control/runtime').mkdir(parents=True)
    (old / 'teleop_control/runtime/parameters.json').write_text('preserved calibration')
    (old / '.env').symlink_to(config / '.env')
    launcher = tmp_path / 'launch'
    launcher.write_text(f'#!/bin/sh\nexec {current}/scripts/package_launcher.py "$@"\n')
    return package, current, data, launcher, config


def test_full_release_replaces_code_and_preserves_state(release):
    package, current, data, launcher, config = release
    result = install(*release)
    work = Path(result['workspace'])
    assert (work / 'scripts/main.py').read_text() == '2.new'
    assert (work / 'teleop_control/runtime/parameters.json').read_text() == 'preserved calibration'
    assert (work / '.env').resolve() == config / '.env'
    assert (config / '.env').read_text() == 'preserved account settings'
    assert (current / 'app/scripts/main.py').read_text() == '1.old'
    assert str(data / 'versions/2.new') in launcher.read_text()
    assert str(current) in (Path(result['backup']) / 'desktop-launcher').read_text()


def test_running_application_prevents_activation(release):
    _, _, data, launcher, _ = release
    before = launcher.read_bytes()
    with (data / 'app.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            install(*release)
    assert launcher.read_bytes() == before
    assert not (data / 'versions/2.new').exists()


def test_unknown_workspace_edits_are_not_discarded(release):
    _, _, data, launcher, _ = release
    before = launcher.read_bytes()
    (data / 'workspaces/1.old/scripts/main.py').write_text('local edit')
    with pytest.raises(ValueError, match='Unexpected local modification'):
        install(*release)
    assert launcher.read_bytes() == before
    assert not (data / 'versions/2.new').exists()
