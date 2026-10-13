import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from install_desktop import APP_ID, desktop_entry, initialize_env, install_shortcuts


def make_app(directory):
    (directory / "scripts").mkdir(parents=True)
    (directory / "public").mkdir()
    (directory / "scripts/desktop_app.py").write_text("print('fixture only')\n")
    (directory / "start-app.sh").write_text("#!/bin/bash\nexec /usr/bin/python3 " + shlex.quote(str(directory / "scripts/desktop_app.py")) + ' "$@"\n')
    (directory / "public/xiaokai-data-collection.svg").write_text("<svg/>")
    return directory


def test_installer_uses_actual_user_and_install_directory(tmp_path):
    app = make_app(tmp_path / "另一位用户" / "应用安装 有空格")
    home = tmp_path / "new-user"
    data = tmp_path / "custom-xdg-data"
    paths = install_shortcuts(app, user_home=home, data_home=data)
    assert str(app / "start-app.sh") in paths["launcher"].read_text()
    assert paths["menu_entry"] == data / "applications" / f"{APP_ID}.desktop"
    assert paths["icon"].read_text() == "<svg/>"
    assert os.access(paths["launcher"], os.X_OK)
    assert "/home/yeap" not in paths["launcher"].read_text()
    assert "/home/yuan" not in paths["launcher"].read_text()


def test_reregister_after_move_replaces_launcher_not_cloud_config(tmp_path):
    first = make_app(tmp_path / "old-location")
    second = make_app(tmp_path / "new-location")
    cloud = second / ".env"
    cloud.write_text("PLATFORM_API_URL=http://192.0.2.2:18000/api\nTOKEN=private\n")
    before = cloud.read_bytes()
    home = tmp_path / "user"
    data = home / ".local/share"
    install_shortcuts(first, user_home=home, data_home=data)
    paths = install_shortcuts(second, user_home=home, data_home=data)
    assert str(second) in paths["launcher"].read_text()
    assert str(first) not in paths["launcher"].read_text()
    assert cloud.read_bytes() == before
    assert "private" not in paths["launcher"].read_text()
    assert "private" not in paths["menu_entry"].read_text()


def test_env_initialization_is_private_and_does_not_reset_existing_settings(tmp_path):
    example = tmp_path / ".env.example"
    example.write_text("PLATFORM_API_URL=http://192.0.2.3:18000/api\n")
    assert initialize_env(tmp_path)
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    example.write_text("PLATFORM_API_URL=http://do-not-overwrite\n")
    assert not initialize_env(tmp_path)
    assert "192.0.2.3" in (tmp_path / ".env").read_text()


def test_missing_app_does_not_modify_shortcuts(tmp_path):
    with pytest.raises(FileNotFoundError):
        install_shortcuts(tmp_path / "absent", user_home=tmp_path, data_home=tmp_path)
    assert not (tmp_path / ".local").exists()


@pytest.mark.skipif(not shutil.which("desktop-file-validate"), reason="desktop validator unavailable")
def test_desktop_entries_validate(tmp_path):
    entry = tmp_path / "sample.desktop"
    entry.write_text(desktop_entry(tmp_path / '用户 folder $name "quote" 100% /launcher'))
    for path in (entry, ROOT / "数据采集台.desktop"):
        subprocess.run(["desktop-file-validate", str(path)], check=True, capture_output=True, text=True)


def system_gio_available():
    return subprocess.run(
        ["/usr/bin/python3", "-c", "from gi.repository import Gio, GLib; assert Gio.DesktopAppInfo"],
        capture_output=True,
    ).returncode == 0


@pytest.mark.skipif(not system_gio_available(), reason="system Gio unavailable")
def test_portable_desktop_entry_expands_current_home_not_developer_home():
    # Parse only: do not launch the real App or change any HOME environment.
    result = subprocess.run(
        ["/usr/bin/python3", "-c",
         "import json,sys; from gi.repository import GLib; k=GLib.KeyFile(); "
         "k.load_from_file(sys.argv[1],GLib.KeyFileFlags.NONE); "
         "print(json.dumps(GLib.shell_parse_argv(k.get_string('Desktop Entry','Exec'))[1]))",
         str(ROOT / "数据采集台.desktop")],
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == [
        "/bin/sh", "-c", 'exec "$HOME/.local/bin/xiaokai-lab-data-collection"'
    ]


@pytest.mark.skipif(not system_gio_available(), reason="system Gio unavailable")
def test_native_launcher_handles_special_characters_without_shell_execution(tmp_path):
    app = make_app(tmp_path / '中文 App $dollar "quote" \'apostrophe\' 100%')
    marker = tmp_path / "launch-result.json"
    (app / "scripts/desktop_app.py").write_text(
        "import json,sys\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text(json.dumps(sys.argv))\n"
    )
    home = tmp_path / 'other user $dollar "quote" 100%'
    paths = install_shortcuts(app, user_home=home, data_home=home / ".local/share")
    result = subprocess.run(
        ["/usr/bin/python3", "-c",
         "import sys; from gi.repository import Gio; "
         "a=Gio.DesktopAppInfo.new_from_filename(sys.argv[1]); "
         "assert a is not None; assert a.launch([],None)",
         str(paths["menu_entry"])],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists(), result.stderr
    assert json.loads(marker.read_text()) == [str(app / "scripts/desktop_app.py")]


def test_vendor_launchers_no_longer_depend_on_named_developer():
    scripts = sorted((ROOT / "teleop_control/vendor/teleop_runtime/apps").glob("*.sh"))
    for script in scripts:
        source = script.read_text()
        assert "/home/yuan" not in source
        assert "/home/yeap" not in source
        subprocess.run(["bash", "-n", str(script)], check=True, capture_output=True)


def test_install_script_syntax_and_automatic_registration():
    script = ROOT / "install-app.sh"
    subprocess.run(["bash", "-n", str(script)], check=True, capture_output=True)
    source = script.read_text()
    assert 'python3 "$APP_ROOT/scripts/install_desktop.py"' in source
    assert "--init-env-only" in source
    assert "install -m 644" not in source
