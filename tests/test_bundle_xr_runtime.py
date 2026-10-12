"""The XR bundle must carry its ABI/dependencies without copying operator state."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bundle_xr_runtime import SERVICE_FILES, SDK_FILENAME, bundle_xr_runtime


def inputs(tmp_path):
    service = tmp_path / "vendor service"
    # Minimal ELF64/amd64 header; packaging validates identity, not executable
    # behavior. Real dynamic loading is exercised in the isolated smoke test.
    header = b"\x7fELF\x02\x01" + b"\0" * 12 + b"\x3e\0" + b"payload"
    for relative in SERVICE_FILES:
        target = service / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(header)
    (service / "setting.ini").write_text("UID=private-user\nConnectedIP=private-address\n")
    (service / "logs").mkdir()
    (service / "logs/session.log").write_text("operator data")
    site = tmp_path / "old python/site-packages"
    site.mkdir(parents=True)
    module = site / SDK_FILENAME
    module.write_bytes(header)
    metadata = site / "xrobotoolkit_sdk-1.0.2.dist-info"
    (metadata / "licenses").mkdir(parents=True)
    (metadata / "METADATA").write_text("Name: xrobotoolkit_sdk\nVersion: 1.0.2\n")
    (metadata / "WHEEL").write_text("Tag: cp310-cp310-linux_x86_64\n")
    (metadata / "licenses/LICENSE").write_text("SDK license")
    (metadata / "direct_url.json").write_text('{"url":"file:///home/private/build"}')
    (metadata / "RECORD").write_text("/home/private/build\n")
    return service, module


def test_bundle_is_self_contained_and_does_not_copy_operator_state(tmp_path):
    service, module = inputs(tmp_path)
    package = tmp_path / "package"
    shared = package / "runtime/robot/python-site"
    shared.mkdir(parents=True)
    (shared / "gdk_dependency.py").write_text("existing GDK dependency")
    manifest = bundle_xr_runtime(package, service, module)
    xr = package / "runtime/robot/xr"
    assert manifest["python_abi"] == "cp310"
    assert manifest["sdk_version"] == "1.0.2"
    assert set(SERVICE_FILES).issubset(
        {str(path.relative_to(xr)) for path in xr.rglob("*") if path.is_file()})
    assert (shared / "gdk_dependency.py").read_text() == "existing GDK dependency"
    assert (shared / SDK_FILENAME).read_bytes() == module.read_bytes()
    assert (shared / "xrobotoolkit_sdk-1.0.2.dist-info/licenses/LICENSE").is_file()
    assert not (shared / "xrobotoolkit_sdk-1.0.2.dist-info/direct_url.json").exists()
    assert not (shared / "xrobotoolkit_sdk-1.0.2.dist-info/RECORD").exists()
    assert not (xr / "logs").exists()
    assert "private" not in (xr / "setting.ini").read_text()
    assert "Protocol=2" in (xr / "setting.ini").read_text()
    assert (xr / "libssl.so").readlink() == Path("libssl.so.3")
    assert (xr / "RoboticsServiceProcess").stat().st_mode & 0o111
    launcher = (xr / "runService.sh").read_text()
    assert "exec ./RoboticsServiceProcess" in launcher
    assert 'QT_PLUGIN_PATH="$XR_DIR/plugins"' in launcher
    assert str(service) not in launcher
    assert "/home/" not in (xr / "bundle-manifest.json").read_text()


def test_wrong_python_abi_fails_before_creating_package(tmp_path):
    service, module = inputs(tmp_path)
    package = tmp_path / "package"
    with pytest.raises(ValueError, match="CPython 3.10"):
        bundle_xr_runtime(package, service, module.with_name("xrobotoolkit_sdk.cpython-312.so"))
    assert not package.exists()


@pytest.mark.parametrize("failure", ["missing", "text", "outside"])
def test_incomplete_or_external_service_dependency_is_rejected(tmp_path, failure):
    service, module = inputs(tmp_path)
    dependency = service / "lib/libQt6Core.so.6"
    dependency.unlink()
    if failure == "text":
        dependency.write_text("not an ELF library")
    elif failure == "outside":
        dependency.symlink_to(module)
    with pytest.raises((ValueError, FileNotFoundError)):
        bundle_xr_runtime(tmp_path / "package", service, module)


def test_shared_python_directory_cannot_redirect_writes_outside_package(tmp_path):
    service, module = inputs(tmp_path)
    package = tmp_path / "package"
    (package / "runtime/robot").mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (package / "runtime/robot/python-site").symlink_to(external)
    with pytest.raises(ValueError, match="escapes the package"):
        bundle_xr_runtime(package, service, module)
    assert not list(external.iterdir())
