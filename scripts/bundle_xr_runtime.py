#!/usr/bin/env python3
"""Stage the native XR service and its CPython 3.10 binding without local state."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil


# Keep the vendor's Qt 6.6 libraries together. The desktop UI uses another Qt
# version; substituting its libraries or plugin directory breaks this service.
SERVICE_FILES = (
    "RoboticsServiceProcess",
    "libBusiness.so",
    "libCommonUtils.so",
    "libDeviceConnectionManager.so",
    "libPXREAGRPCServer.so",
    "libapplogrs.so",
    "libcrypto.so.3",
    "libssl.so.3",
    "libz.so.1",
    "SDK/x64/libPXREARobotSDK.so",
    "lib/libQt6Core.so.6",
    "lib/libQt6Core5Compat.so.6",
    "lib/libQt6Network.so.6",
    "lib/libQt6DBus.so.6",
    "lib/libicui18n.so.56",
    "lib/libicuuc.so.56",
    "lib/libicudata.so.56",
    "plugins/tls/libqopensslbackend.so",
    "plugins/tls/libqcertonlybackend.so",
    "plugins/networkinformation/libqglib.so",
    "plugins/networkinformation/libqnetworkmanager.so",
)
SDK_FILENAME = "xrobotoolkit_sdk.cpython-310-x86_64-linux-gnu.so"

LAUNCHER = '''#!/bin/sh
set -eu
XR_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export LD_LIBRARY_PATH="$XR_DIR:$XR_DIR/lib:$XR_DIR/SDK/x64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export QT_PLUGIN_PATH="$XR_DIR/plugins"
export QT_QML_PATH="$XR_DIR/qml"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"
cd "$XR_DIR"
exec ./RoboticsServiceProcess
'''

# Do not copy the operator's setting.ini: it stores previously connected
# addresses, account identity and remote service settings. These are the local
# TCP/service modes used by the installed vendor reference, without that state.
SETTINGS = '''[ProtocolType]
Protocol=2

[RunMode]
mode=0

[Log]
logToFile=false

[Service]
runAsService=false
'''


def _source_file(path: Path, root: Path, *, elf: bool = False) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError(f"XR source file escapes its input directory: {path.name}")
    if elf:
        with resolved.open("rb") as handle:
            header = handle.read(20)
        if header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\x3e\x00":
            raise ValueError(f"Expected an amd64 ELF XR component: {path.name}")
    return resolved


def bundle_xr_runtime(package: Path, service_root: Path, sdk_module: Path) -> dict:
    """Copy inputs into package/runtime/robot; the caller then patches ELF paths.

    The shared python-site directory is merged with other robot dependencies.
    Source checksums deliberately describe the pre-patchelf input artifacts.
    """
    package = Path(package).resolve()
    service_root = Path(service_root).resolve(strict=True)
    sdk_module = Path(sdk_module).absolute()
    if sdk_module.name != SDK_FILENAME:
        raise ValueError("XR Python binding must have the CPython 3.10 amd64 ABI")
    sdk_root = sdk_module.parent.resolve(strict=True)
    sources = [(relative, _source_file(service_root / relative, service_root, elf=True))
               for relative in SERVICE_FILES]
    binding = _source_file(sdk_module, sdk_root, elf=True)
    metadata_dirs = sorted(sdk_root.glob("xrobotoolkit_sdk-*.dist-info"))
    if len(metadata_dirs) != 1:
        raise ValueError("Expected exactly one XR SDK distribution metadata directory")
    metadata_root = metadata_dirs[0].resolve(strict=True)
    if not metadata_root.is_relative_to(sdk_root):
        raise ValueError("XR SDK metadata escapes its input directory")
    metadata = _source_file(metadata_root / "METADATA", metadata_root).read_text()
    version = next((line.removeprefix("Version: ") for line in metadata.splitlines()
                    if line.startswith("Version: ")), None)
    if not version:
        raise ValueError("XR SDK metadata has no version")

    xr = package / "runtime/robot/xr"
    python_site = package / "runtime/robot/python-site"
    for target in (xr, python_site):
        if not target.resolve().is_relative_to(package):
            raise ValueError("XR destination escapes the package")
        target.mkdir(parents=True, exist_ok=True)
    files = []

    def copy(source: Path, destination: Path) -> None:
        if not destination.resolve().is_relative_to(package):
            raise ValueError("XR destination file escapes the package")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        files.append({"path": str(destination.relative_to(package)),
                      "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                      "source_bytes": source.stat().st_size})

    for relative, source in sources:
        copy(source, xr / relative)
    (xr / "RoboticsServiceProcess").chmod(0o755)
    copy(binding, python_site / SDK_FILENAME)
    # Keep distribution/license metadata, excluding direct_url.json and RECORD
    # from the build environment. No source checkout paths enter the package.
    for relative in ("METADATA", "WHEEL", "top_level.txt"):
        source = metadata_root / relative
        if source.exists():
            copy(_source_file(source, metadata_root),
                 python_site / metadata_dirs[0].name / relative)
    licenses = metadata_root / "licenses"
    if licenses.is_dir():
        for source in sorted(licenses.rglob("*")):
            if source.is_file():
                copy(_source_file(source, metadata_root),
                     python_site / metadata_dirs[0].name / source.relative_to(metadata_root))
    for alias, target in (("libcrypto.so", "libcrypto.so.3"),
                          ("libssl.so", "libssl.so.3"), ("libz.so", "libz.so.1")):
        path = xr / alias
        if path.exists() or path.is_symlink():
            path.unlink()
        path.symlink_to(target)
    (xr / "runService.sh").write_text(LAUNCHER)
    (xr / "runService.sh").chmod(0o755)
    (xr / "setting.ini").write_text(SETTINGS)
    manifest = {"schema_version": 1, "sdk_version": version,
                "python_abi": "cp310", "architecture": "x86_64",
                "service": "runtime/robot/xr/RoboticsServiceProcess",
                "launcher": "runtime/robot/xr/runService.sh", "files": files}
    (xr / "bundle-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--service-root", type=Path, required=True)
    parser.add_argument("--sdk-module", type=Path, required=True)
    args = parser.parse_args()
    result = bundle_xr_runtime(args.package, args.service_root, args.sdk_module)
    print(f"Staged XR SDK {result['sdk_version']} and {len(result['files'])} runtime files")


if __name__ == "__main__":
    main()
