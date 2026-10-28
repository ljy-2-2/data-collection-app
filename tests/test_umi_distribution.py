import hashlib
import json
import platform
import shutil
from pathlib import Path
import sys
import subprocess
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import setup_umi_system as setup
import package_launcher as launcher
from umi_distribution import prepare_umi_distribution, configure_umi_runtime


def resources(tmp_path):
    result = tmp_path / "resources"
    result.mkdir()
    raw = b"fixture image archive"
    (result / "umi-runtime.tar").write_bytes(raw)
    (result / "manifest.json").write_text(json.dumps({
        "architecture": {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}[platform.machine()],
        "image": "xiaokai-umi-runtime:test", "image_id": "sha256:fixture",
        "archive": "umi-runtime.tar", "sha256": hashlib.sha256(raw).hexdigest(),
    }))
    return result


def test_distribution_has_no_capture_time_downloads_or_compilation(tmp_path):
    project = tmp_path / "package/app/umi_collection/vendor/galaxis"
    project.mkdir(parents=True)
    (project / "docker-compose.yml").write_bytes((ROOT / "umi_collection/vendor/galaxis/docker-compose.yml").read_bytes())
    prepare_umi_distribution(tmp_path / "package", resources(tmp_path))
    config = yaml.safe_load((project / "docker-compose.yml").read_text())
    services = config["services"]
    assert set(services) == {"config-init", "galaxis", "galaxis_core", "web_video_server", "galaxis_monitor"}
    for service in services.values():
        assert service["image"] == "xiaokai-umi-runtime:test"
        assert service["pull_policy"] == "never"
        assert "build" not in service
        assert "pip install" not in str(service)
        assert "colcon build" not in str(service)
        bindings = [v for v in service.get("volumes", []) if ":/root/ros2_ws/src/" in v]
        assert all(v.endswith(':ro') and v.split(':')[0] in {
            './galaxis-core/galaxis-core/galaxis_core',
            './galaxis_bringup/galaxis_bringup/encoder_protocol.py',
            './galaxis_bringup/galaxis_bringup/encoder_node.py',
            './galaxis_bringup/galaxis_bringup/camera_devices.py',
            './galaxis_bringup/galaxis_bringup/camera_supervisor.py',
            './galaxis_bringup/galaxis_bringup/camera_control_node.py',
            './galaxis_bringup/galaxis_bringup/center_camera_flip_node.py',
            './galaxis_pico/galaxis_pico_bridge/galaxis_pico_bridge/pico_bridge_node.py',
            './galaxis_pico/galaxis_pico_bridge/galaxis_pico_bridge/transport/galaxis_tcp.py'} for v in bindings)
    assert len([v for v in services['galaxis']['volumes'] if ':/root/ros2_ws/src/' in v]) == 8
    assert len([v for v in services['galaxis_core']['volumes'] if ':/root/ros2_ws/src/' in v]) == 1
    assert "/etc/galaxis:/etc/galaxis:ro" in services["galaxis_core"]["volumes"]
    assert services["config-init"]["entrypoint"] == ["python3"]
    assert "galaxis_studio_bridge" in services["galaxis_monitor"]["command"]
    assert any("runtime_galaxis_devices.yaml" in v for v in services["galaxis"]["volumes"])
    assert ("./galaxis_description/urdf:"
            "/root/ros2_ws/install/galaxis_description/share/galaxis_description/urdf:ro") in services["galaxis"]["volumes"]
    assert (project / ".env").is_file()


def test_incremental_release_reapplies_runtime_after_source_copy(tmp_path):
    project = tmp_path / "package/app/umi_collection/vendor/galaxis"
    project.mkdir(parents=True)
    source = (ROOT / "umi_collection/vendor/galaxis/docker-compose.yml").read_bytes()
    (project / "docker-compose.yml").write_bytes(source)
    assets = resources(tmp_path)
    prepare_umi_distribution(tmp_path / "package", assets)
    (project / "docker-compose.yml").write_bytes(source)
    manifest = json.loads((assets / "manifest.json").read_text())
    configure_umi_runtime(project, manifest)
    configure_umi_runtime(project, manifest)
    services = yaml.safe_load((project / "docker-compose.yml").read_text())["services"]
    for service in services.values():
        assert service["image"] == manifest["image"]
        assert service["pull_policy"] == "never"
        assert "build" not in service
    assert "/etc/galaxis:/etc/galaxis:ro" in services["galaxis_core"]["volumes"]
    assert services['galaxis']['volumes'].count(
        './galaxis_description/urdf:'
        '/root/ros2_ws/install/galaxis_description/share/galaxis_description/urdf:ro') == 1


@pytest.mark.skipif(shutil.which("docker") is None, reason="Compose CLI integration check")
def test_packaged_monitor_compose_resolves_without_host_env_file(tmp_path):
    project = tmp_path / "package/app/umi_collection/vendor/galaxis"
    project.mkdir(parents=True)
    (project / "docker-compose.yml").write_bytes((ROOT / "umi_collection/vendor/galaxis/docker-compose.yml").read_bytes())
    prepare_umi_distribution(tmp_path / "package", resources(tmp_path))
    # Exercise Compose's file resolution, not just YAML syntax: device preflight
    # can pass even when a required env_file makes monitoring fail immediately.
    result = subprocess.run(
        ["docker", "compose", "-f", str(project / "docker-compose.yml"), "config", "--format", "json"],
        capture_output=True, text=True, check=True,
    )
    resolved = json.loads(result.stdout)["services"]
    assert resolved["galaxis"]["environment"]["ROBOT_VARIANT"] == "portable"
    assert resolved["web_video_server"]["environment"]["WEB_VIDEO_SERVER_PORT"] == "9091"
    assert resolved["galaxis_core"]["environment"]["SENTRY_DSN"] == ""


@pytest.mark.parametrize("installed", [True, False])
def test_postinst_loads_verified_runtime_without_starting_collectors(tmp_path, monkeypatch, installed):
    assets = resources(tmp_path)
    assets.rename(tmp_path / "umi-runtime")
    monkeypatch.setattr(setup, "installer_user", lambda _: "operator")
    monkeypatch.setattr(setup, "_group_exists", lambda _: True)
    calls = []
    loaded = installed

    def run(command, **kwargs):
        nonlocal loaded
        calls.append(command)
        if command[:2] == ["docker", "load"]:
            loaded = True
        if command[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(returncode=0 if loaded else 1, stdout="sha256:fixture" if loaded else "")
        return SimpleNamespace(returncode=0, stdout="")

    setup.setup(tmp_path, run=run)
    assert any(c[:2] == ["docker", "load"] for c in calls) is not installed
    assert ["usermod", "-a", "-G", "docker,dialout,video,plugdev", "operator"] in calls
    assert all(c[:2] != ["docker", "run"] and "up" not in c for c in calls)


def test_bad_runtime_checksum_cannot_modify_system(tmp_path):
    assets = resources(tmp_path)
    assets.rename(tmp_path / "umi-runtime")
    (tmp_path / "umi-runtime/umi-runtime.tar").write_bytes(b"damaged")
    with pytest.raises(RuntimeError, match="校验失败"):
        setup.setup(tmp_path, run=lambda *a, **k: pytest.fail("system changed before validation"))


def test_installer_never_guesses_or_grants_all_local_users():
    assert setup.installer_user({}) is None
    assert setup.installer_user({"SUDO_UID": "0"}) is None


@pytest.mark.parametrize("standalone", [False, True])
@pytest.mark.parametrize("existing_groups", [[902, 903, 904], []])
def test_launcher_refreshes_new_group_membership_without_root_app(tmp_path, monkeypatch, standalone, existing_groups):
    import shlex
    (tmp_path / "umi-runtime").mkdir()
    (tmp_path / "umi-runtime/manifest.json").write_text("{}")
    if standalone:
        (tmp_path / "self-contained.json").write_text("{}")
    monkeypatch.setattr(launcher.pwd, "getpwuid", lambda _: SimpleNamespace(pw_name="operator", pw_gid=1000))
    monkeypatch.setattr(launcher.grp, "getgrgid", lambda gid: SimpleNamespace(gr_name="operator", gr_gid=gid))
    runtime_group = "xiaokai-collection" if standalone else "docker"
    ids = {runtime_group: 901, "dialout": 902, "video": 903, "plugdev": 904}
    monkeypatch.setattr(launcher.grp, "getgrnam", lambda name: SimpleNamespace(gr_name=name, gr_gid=ids[name], gr_mem=["operator"]))
    monkeypatch.setattr(launcher.os, "getgroups", lambda: existing_groups)
    monkeypatch.setattr(launcher.os, "getegid", lambda: 1000)
    monkeypatch.delenv("XIAOKAI_GROUP_REFRESHED", raising=False)
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: pytest.fail("already authorized user should not request root"))
    commands = []
    monkeypatch.setattr(launcher.os, "execv", lambda path, args: commands.append((path, args)))
    launcher.ensure_collection_permissions(tmp_path)
    assert commands[0][1][:3] == ["/usr/bin/sg", runtime_group, "-c"]
    python = str(tmp_path / "runtime/python/bin/python3.12") if standalone else "/usr/bin/python3"
    assert python in commands[0][1][3]
    command = commands[0][1]
    switched_groups = []
    while command[0] == "/usr/bin/sg":
        switched_groups.append(command[1])
        assert command[2] == "-c"
        command = shlex.split(command[3])
        assert command.pop(0) == "exec"
    assert switched_groups == [name for name, gid in ids.items() if gid not in existing_groups] + ["operator"]
    assert command[:2] == [python, str(tmp_path / "scripts/package_launcher.py")]


def test_failed_permission_refresh_cannot_loop(tmp_path, monkeypatch):
    (tmp_path / "umi-runtime").mkdir()
    (tmp_path / "umi-runtime/manifest.json").write_text("{}")
    monkeypatch.setattr(launcher.pwd, "getpwuid", lambda _: SimpleNamespace(pw_name="operator"))
    monkeypatch.setattr(launcher.grp, "getgrnam", lambda name: SimpleNamespace(gr_name=name, gr_gid=901, gr_mem=["operator"]))
    monkeypatch.setattr(launcher.os, "getgroups", lambda: [])
    monkeypatch.setattr(launcher.os, "getegid", lambda: 1000)
    monkeypatch.setenv("XIAOKAI_GROUP_REFRESHED", "1")
    with pytest.raises(RuntimeError, match="注销"):
        launcher.ensure_collection_permissions(tmp_path)


@pytest.mark.parametrize('arch,machine',[('amd64',62),('arm64',183)])
def test_camera_overlay_selects_architecture_and_rejects_corruption(tmp_path,arch,machine):
    project=tmp_path/'project';project.mkdir()
    (project/'docker-compose.yml').write_bytes((ROOT/'umi_collection/vendor/galaxis/docker-compose.yml').read_bytes())
    lib=project/f'third_party/usb_cam/native/{arch}/libusb_cam.so';lib.parent.mkdir(parents=True)
    raw=bytearray(64);raw[:6]=b'\x7fELF\x02\x01';raw[18:20]=machine.to_bytes(2,'little');lib.write_bytes(raw)
    (lib.parent/'build.json').write_text(json.dumps(dict(architecture=arch,sha256=hashlib.sha256(raw).hexdigest())))
    manifest=dict(image='native-test',architecture=arch)
    configure_umi_runtime(project,manifest);configure_umi_runtime(project,manifest)
    bindings=yaml.safe_load((project/'docker-compose.yml').read_text())['services']['galaxis']['volumes']
    camera=[b for b in bindings if 'install/usb_cam/lib/libusb_cam.so:' in b]
    assert camera==[f'./third_party/usb_cam/native/{arch}/libusb_cam.so:/root/ros2_ws/install/usb_cam/lib/libusb_cam.so:ro']
    lib.write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='架构或校验'):configure_umi_runtime(project,manifest)
