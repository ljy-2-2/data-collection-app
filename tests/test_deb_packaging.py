import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from build_deb import ignore_private, package_defaults, relocate_python_links  # noqa: E402
from package_launcher import prepare  # noqa: E402
from sanitize_package import clear_secrets  # noqa: E402


def test_distribution_only_inherits_public_urls():
    defaults = package_defaults(
        {
            "PLATFORM_API_URL": "https://platform.example/api",
            "PLATFORM_WEB_URL": "https://platform.example",
            "PASSWORD": "private",
            "PLATFORM_TOKEN_FILE": "/home/private/token",
            "TELEOP_GDK_PYTHON": "/home/private/python",
        }
    )
    assert "https://platform.example/api" in defaults
    assert "private" not in defaults
    assert "TELEOP_ROBOT_INTERFACE" not in defaults


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@example.com",
        "file:///etc/passwd",
        "https://example.com?token=secret",
        "https://example.com\nPASSWORD=x",
    ],
)
def test_distribution_rejects_credentials_in_url(url):
    with pytest.raises(ValueError):
        package_defaults({"PLATFORM_API_URL": url})


def test_private_files_excluded():
    names = [
        ".env",
        "runtime",
        "_runtime",
        "local",
        "platform-auth.json",
        ".env.production",
        ".env.example",
        "main.py",
    ]
    ignored = ignore_private("config", names)
    assert set(ignored) == set(names[:-2])


def test_packaged_python_alias_survives_removing_build_cache(tmp_path):
    import shutil

    source = tmp_path / "builder-cache"
    (source / "bin").mkdir(parents=True)
    (source / "bin/python3.12").write_text("private interpreter")
    (source / "bin/python").symlink_to(source / "bin/python3.12")
    (source / "bin/python3").symlink_to("python3.12")
    package = tmp_path / "recipient/runtime/python"
    shutil.copytree(source, package, symlinks=True)
    relocate_python_links(source, package)
    shutil.rmtree(source)
    assert (package / "bin/python").read_text() == "private interpreter"
    assert (package / "bin/python3").read_text() == "private interpreter"
    assert (package / "bin/python").readlink() == Path("python3.12")


@pytest.mark.parametrize("target", ["/usr/bin/python3", "../../../../builder-only/python"])
def test_packaged_python_rejects_external_interpreter_links(tmp_path, target):
    package = tmp_path / "runtime/python"
    (package / "bin").mkdir(parents=True)
    (package / "bin/python").symlink_to(target)
    with pytest.raises(ValueError, match="runtime"):
        relocate_python_links(tmp_path / "cache", package)


def test_vendor_secrets_cleared_without_changing_other_config():
    assert clear_secrets(
        {
            "config": {
                "api_key": "secret",
                "s3_secret_access_key": "secret",
                "record_format": "hdf5",
            }
        }
    ) == {
        "config": {"api_key": "", "s3_secret_access_key": "", "record_format": "hdf5"}
    }


def test_no_g2_sdk_does_not_block_app_startup(tmp_path, monkeypatch):
    from teleop_control import main as control

    monkeypatch.setattr(
        control, "__file__", str(tmp_path / "teleop_control/src/teleop_control/main.py")
    )
    monkeypatch.setattr(sys, "argv", ["teleop-control"])
    args = control.parse_args()
    args.gdk_app_root = str(tmp_path / "missing-sdk")
    args.gdk_python = None
    controller = control.build_controller(args)
    robot = controller.detector.check_robot()
    assert robot.status == "offline"
    assert "SDK" in robot.detail
    assert args.gdk_python == sys.executable
    assert control.build_preview(args) is None


def test_user_work_copy_and_config_survive_upgrade(tmp_path):
    package = tmp_path / "read-only-package"
    (package / "app/scripts").mkdir(parents=True)
    (package / "app/scripts/example.py").write_text("first")
    (package / "defaults.env").write_text(
        "PLATFORM_API_URL=https://initial.example/api\n"
    )
    (package / "package-build.json").write_text(json.dumps({"build_id": "v1"}))
    data, config = tmp_path / "用户 数据", tmp_path / "用户 配置"
    work = prepare(package, data, config)
    assert (work / ".env").resolve() == config / ".env"
    assert (config / ".env").stat().st_mode & 0o777 == 0o600
    assert (work / "scripts/example.py").read_text() == "first"
    (config / ".env").write_text("PLATFORM_API_URL=https://user.example/api\n")
    (work / "runtime-marker").write_text("old session")
    (package / "package-build.json").write_text(json.dumps({"build_id": "v2"}))
    new_work = prepare(package, data, config)
    assert not (new_work / "runtime-marker").exists()
    assert "user.example" in (new_work / ".env").read_text()
    assert not (package / "app/.env").exists()
