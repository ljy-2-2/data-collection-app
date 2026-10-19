"""Keep private Docker on the same containerd store across service restarts."""
import configparser
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_self_contained_deb as build
import setup_self_contained as setup


@pytest.fixture
def stage(tmp_path):
    build.write_private_container_runtime(tmp_path)
    return tmp_path


def unit(stage, name):
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    config.read(stage / "usr/lib/systemd/system" / name)
    return config


def test_containerd_uses_previous_private_store_without_switching_driver(stage):
    runtime = stage / str(build.PREFIX).lstrip("/") / "runtime"
    docker = json.loads((runtime / "docker-daemon.json").read_text())
    assert docker["containerd"] == "/run/xiaokai-collection/exec/containerd/containerd.sock"
    assert docker["data-root"] == "/var/lib/xiaokai-lab-data-collection/docker"
    assert docker["containerd-namespace"] == "moby"
    assert docker["containerd-plugins-namespace"] == "plugins.moby"
    assert "storage-driver" not in docker
    assert "features" not in docker  # Do not switch the existing snapshotter.
    text = (runtime / "containerd.toml").read_text()
    assert 'root = "/var/lib/xiaokai-lab-data-collection/docker/containerd/daemon"' in text
    assert 'state = "/run/xiaokai-collection/exec/containerd/daemon"' in text
    assert f'address = "{docker["containerd"]}"' in text
    assert "imports = []" in text


def test_dockerd_restart_cannot_remove_containerd_socket_or_state(stage):
    docker = unit(stage, setup.DOCKER_SERVICE)
    containerd = unit(stage, setup.CONTAINERD_SERVICE)
    assert setup.CONTAINERD_SERVICE in docker["Unit"]["Requires"].split()
    assert setup.CONTAINERD_SERVICE in docker["Unit"]["After"].split()
    assert "RuntimeDirectory" not in docker["Service"]
    assert containerd["Service"]["RuntimeDirectory"] == "xiaokai-collection"
    assert containerd["Service"]["RuntimeDirectoryPreserve"] == "yes"
    assert containerd["Service"]["Type"] == "notify"
    assert containerd["Service"]["KillMode"] == "process"
    assert "ExecStartPre" not in containerd["Service"]  # No deleting old state.
    assert containerd["Service"]["ExecStart"].startswith(str(build.PREFIX / "runtime/bin/containerd"))


@pytest.mark.parametrize("with_avahi", [False, True])
def test_setup_stops_docker_before_restarting_private_store(monkeypatch, with_avahi):
    commands = []
    monkeypatch.setattr(setup, "run", lambda *args: commands.append(args))
    selected = [setup.CONTAINERD_SERVICE, setup.DOCKER_SERVICE]
    if with_avahi:
        selected.append(setup.AVAHI_SERVICE)
    setup.restart_services(selected)
    expected = [
        ("/usr/bin/systemctl", "stop", setup.DOCKER_SERVICE),
        ("/usr/bin/systemctl", "restart", setup.CONTAINERD_SERVICE),
        ("/usr/bin/systemctl", "start", setup.DOCKER_SERVICE),
    ]
    if with_avahi:
        expected.append(("/usr/bin/systemctl", "restart", setup.AVAHI_SERVICE))
    assert commands == expected


def test_containerd_start_failure_does_not_start_docker_on_host_store(monkeypatch):
    commands = []

    def run(*args):
        commands.append(args)
        if args[-1] == setup.CONTAINERD_SERVICE:
            raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(setup, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        setup.restart_services(setup.SERVICES)
    assert len(commands) == 2
    assert not any("start" in command for command in commands)


def test_upgrade_and_remove_manage_only_private_services_and_keep_data(stage):
    prerm = (stage / "DEBIAN/prerm").read_text()
    postrm = (stage / "DEBIAN/postrm").read_text()
    assert '"$1" = upgrade' in prerm
    for service in setup.SERVICES:
        assert service in prerm
        assert "/etc/systemd/system/multi-user.target.wants/" + service in postrm
    assert "systemctl stop docker.service" not in prerm
    assert "systemctl stop containerd.service" not in prerm
    assert "rm -rf" not in prerm + postrm
    assert "/var/lib/" not in prerm + postrm
    for name in ("postinst", "prerm", "postrm"):
        path = stage / "DEBIAN" / name
        assert path.stat().st_mode & 0o111 == 0o111
        subprocess.run(["sh", "-n", str(path)], check=True)


def test_incremental_staging_keeps_application_and_external_units(stage):
    app = stage / str(build.PREFIX).lstrip("/") / "app/saved-source.py"
    app.parent.mkdir(parents=True)
    app.write_text("preserve current application")
    external = stage / "usr/lib/systemd/system/docker.service"
    external.write_text("unrelated host unit")
    build.write_private_container_runtime(stage)
    assert app.read_text() == "preserve current application"
    assert external.read_text() == "unrelated host unit"
