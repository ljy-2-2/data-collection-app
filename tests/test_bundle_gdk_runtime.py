"""Exercise portable G2 packaging without importing a robot SDK."""
import csv
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bundle_gdk_runtime as bundle


def wheel(site, name, files, dependencies=(), version="1.0"):
    metadata = f"{name.replace('-', '_')}-{version}.dist-info"
    entries = dict(files)
    entries[f"{metadata}/METADATA"] = (
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        + "".join(f"Requires-Dist: {dep}\n" for dep in dependencies)
    )
    for relative, contents in entries.items():
        path = site / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents if isinstance(contents, bytes) else contents.encode())
    record = site / metadata / "RECORD"
    with record.open("w") as handle:
        csv.writer(handle).writerows((name, "", "") for name in [*entries, f"{metadata}/RECORD"])


def test_python_closure_uses_target_310_markers_extras_and_nested_cmeel(tmp_path):
    site = tmp_path / "python"
    wheel(site, "worker", {"worker/__init__.py": ""}, [
        'older[feature]>=1; python_version < "3.11"',
        'not_installed; python_version >= "3.11"',
    ])
    wheel(site, "older", {"older.py": ""}, ['leaf; extra == "feature"'])
    wheel(site, "leaf", {"leaf.py": ""})
    wheel(site, "unused", {"unused.py": ""})
    wheel(site, "pin", {"cmeel.prefix/lib/python3.10/site-packages/pinocchio/__init__.py": ""})
    selected = bundle.select_distributions([site], ["worker", "pinocchio"])
    assert set(selected) == {"worker", "older", "leaf", "pin"}
    output = tmp_path / "output"
    bundle.copy_distributions(selected, output)
    assert (output / "leaf.py").exists()
    assert not (output / "unused.py").exists()


def test_missing_python_dependency_fails_with_its_name(tmp_path):
    wheel(tmp_path, "worker", {"worker.py": ""}, ["missing>=2"])
    with pytest.raises(RuntimeError, match="missing>=2"):
        bundle.select_distributions([tmp_path], ["worker"])


def test_python_copy_preserves_runtime_sidecars_but_excludes_build_state(tmp_path):
    source = tmp_path / "source"
    wheel(source, "worker", {
        "worker.py": "", "worker.libs/data.bin": b"runtime",
        "worker-1.0.dist-info/direct_url.json": '{"url":"file:///home/private/build"}',
        "cmeel.prefix/include/dependency.hpp": "header",
        "cmeel.prefix/lib/cmake/dependency.cmake": "build-only",
        "cmeel.prefix/share/package/data.xml": "runtime asset",
        "cmeel.pth": "import cmeel_pth\n",
    })
    output = tmp_path / "output"
    bundle.copy_distributions(bundle.select_distributions([source], ["worker"]), output)
    assert (output / "worker.libs/data.bin").read_bytes() == b"runtime"
    assert (output / "cmeel.prefix/share/package/data.xml").is_file()
    assert (output / "cmeel.pth").is_file()
    assert not (output / "worker-1.0.dist-info/direct_url.json").exists()
    assert not (output / "cmeel.prefix/include").exists()
    assert not (output / "cmeel.prefix/lib/cmake").exists()


@pytest.mark.parametrize("relative,value,match", [
    ("worker.cpython-312-x86_64-linux-gnu.so", b"bad abi", "Wrong Python ABI"),
    ("worker.pth", "/home/private/conda/lib\n", "Nonportable Python path"),
])
def test_wrong_abi_or_nonportable_hooks_are_rejected(tmp_path, relative, value, match):
    source = tmp_path / "source"
    wheel(source, "worker", {"worker.py": "", relative: value})
    with pytest.raises(ValueError, match=match):
        bundle.copy_distributions(bundle.select_distributions([source], ["worker"]), tmp_path / "output")


def shared_library(path, code, *arguments):
    if not shutil.which("cc") or not shutil.which("readelf"):
        pytest.skip("C compiler and readelf required for ELF closure test")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cc", "-shared", "-fPIC", "-nostdlib", "-x", "c", "-",
                    "-o", str(path), *arguments], input=code, text=True, check=True,
                   capture_output=True)
    return path


def native_fixture(tmp_path):
    libs = tmp_path / "vendor"
    real = shared_library(libs / "libfixture.so.1.2", "int fixture(void) { return 7; }",
                          "-Wl,-soname,libfixture.so.1")
    (libs / "libfixture.so.1").symlink_to(real.name)
    (libs / "libfixture.so").symlink_to(real.name)
    seed = shared_library(tmp_path / "worker.so", "extern int fixture(void); int worker(void) { return fixture(); }",
                          f"-L{libs}", "-lfixture")
    shared_library(libs / "libunused.so", "int unused(void) { return 9; }")
    return libs, seed


def test_actual_elf_closure_materializes_only_used_library_with_relative_soname(tmp_path):
    libs, seed = native_fixture(tmp_path)
    output = tmp_path / "output"
    staged = bundle.copy_native_closure([seed], output, [libs], [])
    assert {path.name for path in staged} == {"libfixture.so.1", "libfixture.so.1.2"}
    assert (output / "libfixture.so.1").readlink() == Path("libfixture.so.1.2")
    assert not (output / "libunused.so").exists()
    assert bundle.elf_info(seed).needed == ("libfixture.so.1",)


def test_existing_private_library_is_reused_and_missing_library_fails(tmp_path):
    libs, seed = native_fixture(tmp_path)
    assert bundle.copy_native_closure([seed], tmp_path / "output", [libs], [libs]) == []
    with pytest.raises(RuntimeError, match="Unresolved native dependency libfixture.so.1"):
        bundle.copy_native_closure([seed], tmp_path / "output", [], [])


def test_later_stronger_abi_requirement_upgrades_provider_before_copy(tmp_path, monkeypatch):
    paths = {name: tmp_path / name for name in ("first.so", "later.so", "old.so", "new.so")}
    for name, path in paths.items():
        path.write_bytes(name.encode())
    details = {
        "first.so": bundle.ElfInfo(("libabi.so",), {"libabi.so": {"ABI_1"}}, set()),
        "later.so": bundle.ElfInfo(("libabi.so",), {"libabi.so": {"ABI_2"}}, set()),
        "old.so": bundle.ElfInfo((), {}, {"ABI_1"}),
        "new.so": bundle.ElfInfo((), {}, {"ABI_1", "ABI_2"}),
    }
    monkeypatch.setattr(bundle, "elf_info", lambda path: details[path.name])
    monkeypatch.setattr(bundle, "_native_candidates", lambda roots: {"libabi.so": [paths["old.so"], paths["new.so"]]})
    staged = bundle.copy_native_closure([paths["first.so"], paths["later.so"]], tmp_path / "output", [], [])
    assert [path.name for path in staged] == ["new.so"]


def test_config_relocates_all_vendor_paths_without_fabricating_calibration(tmp_path):
    source = tmp_path / "sdk"
    config = source / "gdk/config"
    config.mkdir(parents=True)
    (config / "app_conf.json").write_text(
        '{"gdk_config_path":"~/.cache/agibot/app/gdk/config/",'
        '"cam_dev_conf_path":"/home/agi/app/conf/deploy/develop/"}')
    (config / "policy.yaml").write_text("policy: /home/agi/app/gdk/config/remote.yaml\n")
    deployed = source / "conf/deploy/develop"
    deployed.mkdir(parents=True)
    (deployed / "camera.json").write_text("{}")
    target = tmp_path / "output"
    installed = bundle.INSTALL_PREFIX / "runtime/robot/gdk"
    missing = bundle.copy_configuration(source, target, installed)
    assert missing == ["data/parameters/sensor"]
    assert not (target / "data/parameters/sensor").exists()
    assert (target / "conf/deploy/develop/camera.json").is_file()
    text = (target / "gdk/config/app_conf.json").read_text()
    assert str(installed) in text and "/home/" not in text and "~/.cache" not in text
    assert str(installed) in (target / "gdk/config/policy.yaml").read_text()


def test_bundle_preserves_shared_xr_site_and_records_relative_paths(tmp_path):
    libs, seed = native_fixture(tmp_path)
    sdk = tmp_path / "sdk"
    shutil.copytree(libs, sdk / "lib", symlinks=True)
    shared_library(sdk / "lib/libplugin_dependency.so", "int plugin_dependency(void) { return 11; }",
                   "-Wl,-soname,libplugin_dependency.so")
    for name in bundle.DYNAMIC_PLUGINS:
        shared_library(sdk / "lib" / name,
                       "extern int plugin_dependency(void); int plugin(void) { return plugin_dependency(); }",
                       f"-L{sdk / 'lib'}", "-lplugin_dependency")
    extension = sdk / "gdk/lib/agibot_gdk/agibot_gdk.cpython-310-x86_64-linux-gnu.so"
    extension.parent.mkdir(parents=True)
    shutil.copy2(seed, extension)
    (sdk / "gdk/config").mkdir()
    (sdk / "gdk/config/app_conf.json").write_text("{}")
    site = tmp_path / "site"
    wheel(site, "worker", {"worker.py": ""})
    package = tmp_path / "package"
    destination = package / "runtime/robot/python-site"
    destination.mkdir(parents=True)
    (destination / "xrobotoolkit_sdk.so").write_bytes(b"existing XR")
    report = bundle.bundle_gdk_runtime(package, sdk, [site], required_modules=["worker"])
    assert report["python_distributions"] == {"worker": "1.0"}
    assert (destination / "xrobotoolkit_sdk.so").read_bytes() == b"existing XR"
    assert report["native_libraries"]
    assert {Path(path).name for path in report["dynamic_plugins"]} == set(bundle.DYNAMIC_PLUGINS)
    assert (package / "runtime/robot/gdk/lib/libplugin_dependency.so").is_file()
    for name in bundle.DYNAMIC_PLUGINS:
        assert (package / "runtime/robot/gdk/lib" / name).is_file()
    assert all(not Path(path).is_absolute() for path in report["native_libraries"])
    assert not (package / "runtime/robot/gdk/lib/libunused.so").exists()


def test_missing_dynamic_communication_plugin_prevents_incomplete_package(tmp_path):
    sdk = tmp_path / "sdk"
    extension = sdk / "gdk/lib/agibot_gdk/agibot_gdk.cpython-310-x86_64-linux-gnu.so"
    shared_library(extension, "int worker(void) { return 0; }")
    (sdk / "gdk/config").mkdir()
    (sdk / "lib").mkdir()
    package = tmp_path / "package"
    with pytest.raises(FileNotFoundError, match="dynamic communication plugin"):
        bundle.bundle_gdk_runtime(package, sdk, [], required_modules=[])
    assert not package.exists()
