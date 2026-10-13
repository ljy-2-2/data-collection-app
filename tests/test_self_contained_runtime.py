"""Docker can return success while image unpacking actually failed."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import setup_self_contained as setup


def test_partial_docker_load_never_marks_image_ready(tmp_path, monkeypatch):
    ready = tmp_path / "image-ready"
    monkeypatch.setattr(setup, "PACKAGE", tmp_path)
    monkeypatch.setattr(setup, "Path", lambda _: ready)
    monkeypatch.setattr(setup, "verified_manifest", lambda: {
        "image": "collection:test", "image_id": "sha256:test", "archive": "image.tar",
    })
    calls = []

    def invoke(command, **kwargs):
        calls.append(command)
        if "load" in command:
            return SimpleNamespace(returncode=0, stdout="Loaded image: collection:test\nError unpacking image: permission denied\n", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="not found")

    monkeypatch.setattr(setup.subprocess, "run", invoke)
    with pytest.raises(RuntimeError, match="解包失败"):
        setup.load_image()
    assert not ready.exists()
    assert sum("load" in call for call in calls) == 1
