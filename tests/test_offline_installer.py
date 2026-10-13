"""Preflight failures must stop before requesting privilege or running APT."""
import hashlib
from pathlib import Path
import shutil
import subprocess


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/offline_install.sh"


def bundle(tmp_path):
    shutil.copy2(SCRIPT, tmp_path / "install.sh")
    (tmp_path / "bundle.env").write_text(
        "TARGET_UBUNTU=unsupported-test-release\nTARGET_ARCH=amd64\n"
        "APP_PACKAGE=xiaokai-lab-data-collection\nAPP_VERSION=1\n"
    )
    (tmp_path / "SHA256SUMS").write_text("".join(
        f"{hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()}  {name}\n"
        for name in ("install.sh", "bundle.env")
    ))
    return tmp_path / "install.sh"


def test_mismatched_release_stops_before_apt(tmp_path):
    script = bundle(tmp_path)
    result = subprocess.run(["bash", str(script), "--check"], capture_output=True, text=True)
    assert result.returncode == 1
    assert "当前系统不匹配，未安装" in result.stderr
    assert "正在读取" not in result.stdout


def test_corruption_stops_before_loading_settings(tmp_path):
    script = bundle(tmp_path)
    with (tmp_path / "bundle.env").open("a") as handle:
        handle.write("echo UNVERIFIED_CONFIGURATION_EXECUTED\n")
    result = subprocess.run(["bash", str(script), "--check"], capture_output=True, text=True)
    assert result.returncode == 1
    assert "安装文件损坏或不完整" in result.stderr
    assert "UNVERIFIED_CONFIGURATION_EXECUTED" not in result.stdout
