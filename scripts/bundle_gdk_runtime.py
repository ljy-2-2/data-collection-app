#!/usr/bin/env python3
"""Stage a Python 3.10 G2 runtime without importing a robot SDK or copying Conda."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from typing import Sequence

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


DEFAULT_MODULES = (
    "numpy", "scipy", "pydantic", "yaml", "cv2", "h5py", "google.protobuf",
    "pinocchio", "PIL", "serial", "requests", "psutil", "tqdm", "packaging",
)
INSTALL_PREFIX = Path("/opt/xiaokai-lab-data-collection")
# These are loaded with dlopen rather than linked into DT_NEEDED. Aorta's
# ResourceLoader constructs its module name; Cosine names the DDS transport and
# IDL/protobuf bridge in its plugin loader. Missing any can leave import
# successful while robot communication has failed to initialize.
DYNAMIC_PLUGINS = (
    "libdynamic_aorta.so", "libcosine_bus_agibotdds.so", "libidl_proto_bridge.so",
)


@dataclass
class ElfInfo:
    needed: tuple[str, ...]
    requires: dict[str, set[str]]
    provides: set[str]


def is_elf(path: Path) -> bool:
    if not path.is_file():
        return False
    with path.open("rb") as handle:
        return handle.read(4) == b"\x7fELF"


def elf_info(path: Path) -> ElfInfo:
    """Read dependencies/version requirements; never execute the binary."""
    with path.open("rb") as handle:
        header = handle.read(20)
    if header[:6] != b"\x7fELF\x02\x01" or int.from_bytes(header[18:20], "little") != 62:
        raise ValueError(f"Robot runtime requires x86_64 ELF: {path}")
    env = {**os.environ, "LC_ALL": "C"}
    dynamic = subprocess.check_output(["readelf", "-d", str(path)], text=True, env=env)
    versions = subprocess.check_output(["readelf", "-V", str(path)], text=True, env=env)
    needed = tuple(re.findall(r"\(NEEDED\).*?\[(.*?)\]", dynamic))
    requires: dict[str, set[str]] = defaultdict(set)
    provides: set[str] = set()
    section = current = ""
    for line in versions.splitlines():
        if line.startswith("Version needs section"):
            section = "needs"
        elif line.startswith("Version definition section"):
            section = "definitions"
        elif line.startswith("Version symbols section"):
            section = "symbols"
        match = re.search(r"\bFile: (\S+)", line)
        if section == "needs" and match:
            current = match[1]
        match = re.search(r"\bName: (\S+)", line)
        if match and section == "needs" and current:
            requires[current].add(match[1])
        elif match and section == "definitions":
            provides.add(match[1])
    return ElfInfo(needed, dict(requires), provides)


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(target.read_bytes()).digest():
            raise ValueError(f"Conflicting staged robot dependency: {target}")
        return
    # Materialize Python-package symlinks so no build-machine link survives.
    shutil.copy2(source, target, follow_symlinks=True)


def _owns_module(dist: importlib.metadata.Distribution, module: str) -> bool:
    expected = module.split(".")
    for file in dist.files or ():
        parts = list(PurePosixPath(str(file)).parts)
        if "site-packages" in parts:
            parts = parts[parts.index("site-packages") + 1:]
        if not parts or ".." in parts:
            continue
        # Handles both packages and abi3/cpython extension modules such as cv2.
        if len(parts) >= len(expected):
            candidate = parts[:len(expected)]
            candidate[-1] = candidate[-1].split(".")[0]
            if candidate == expected:
                return True
    return False


def select_distributions(python_sites: Sequence[Path], modules: Sequence[str]):
    """Resolve wheel metadata for CPython 3.10, independent of build Python."""
    distributions = {}
    for site in python_sites:
        if not site.is_dir():
            raise FileNotFoundError(f"Python 3.10 site-packages missing: {site}")
        for dist in importlib.metadata.distributions(path=[str(site)]):
            name = dist.metadata.get("Name")
            if name:
                distributions.setdefault(canonicalize_name(name), dist)
    pending = deque()
    for module in modules:
        owners = [name for name, dist in distributions.items() if _owns_module(dist, module)]
        if not owners:
            raise RuntimeError(f"Required Python 3.10 module is not installed: {module}")
        pending.extend((name, set()) for name in owners)
    environment = {
        **default_environment(), "python_version": "3.10", "python_full_version": "3.10.12",
        "implementation_name": "cpython", "implementation_version": "3.10.12",
        "platform_python_implementation": "CPython", "platform_machine": "x86_64",
        "sys_platform": "linux", "os_name": "posix", "platform_system": "Linux", "extra": "",
    }
    selected = {}
    processed = set()
    while pending:
        name, extras = pending.popleft()
        state = name, tuple(sorted(extras))
        if state in processed:
            continue
        processed.add(state)
        dist = distributions.get(name)
        if dist is None:
            raise RuntimeError(f"Python 3.10 dependency is not installed: {name}")
        selected[name] = dist
        for text in dist.requires or ():
            requirement = Requirement(text)
            if requirement.marker and not any(
                requirement.marker.evaluate({**environment, "extra": extra}) for extra in (extras or {""})
            ):
                continue
            dependency = canonicalize_name(requirement.name)
            available = distributions.get(dependency)
            if available is None or not requirement.specifier.contains(available.version, prereleases=True):
                raise RuntimeError(f"Unsatisfied Python 3.10 dependency for {name}: {requirement}")
            pending.append((dependency, set(requirement.extras)))
    return selected


def copy_distributions(distributions, destination: Path) -> list[Path]:
    binaries = []
    for name, dist in sorted(distributions.items()):
        if not dist.files:
            raise RuntimeError(f"Distribution has no installed file manifest: {name}")
        for file in dist.files:
            relative = PurePosixPath(str(file))
            if relative.is_absolute() or ".." in relative.parts:
                continue  # Wheel console scripts are not worker dependencies.
            if "__pycache__" in relative.parts or relative.suffix in (".pyc", ".pyo"):
                continue
            if relative.name == "direct_url.json":
                continue  # Build provenance can contain local paths or URL tokens.
            if relative.parts[0] == "cmeel.prefix" and (
                relative.parts[1] in ("include", "bin")
                or "cmake" in relative.parts or "pkgconfig" in relative.parts
                or relative.suffix == ".a"
            ):
                continue  # C/C++ build files are not used by the robot workers.
            source = Path(dist.locate_file(file))
            if not source.is_file():
                raise FileNotFoundError(f"Distribution {name} file missing: {relative}")
            abi = re.search(r"\.cpython-(\d+)-", relative.name)
            if abi and abi[1] != "310":
                raise ValueError(f"Wrong Python ABI in robot runtime: {relative}")
            if relative.suffix in (".pth", ".egg-link"):
                for line in source.read_text().splitlines():
                    if line.strip().startswith(("/", "file:")):
                        raise ValueError(f"Nonportable Python path hook: {relative}")
            target = destination / relative
            _copy_file(source, target)
            if is_elf(target):
                binaries.append(target)
    return binaries


def _native_candidates(roots: Sequence[Path]) -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = defaultdict(list)
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.so*")):
            if path.is_file() and path not in result[path.name] and is_elf(path):
                result[path.name].append(path)
    return dict(result)


def copy_native_closure(seeds: Sequence[Path], destination: Path, search_roots: Sequence[Path],
                        provided_roots: Sequence[Path]) -> list[Path]:
    """Copy only referenced SONAMEs, using ABI-compatible existing libraries."""
    candidates = _native_candidates(search_roots)
    provided = [path.resolve() for path in provided_roots]
    pending = deque(seeds)
    info_cache = {}
    visited = set()
    staged = set()
    selected = {}
    requirements: dict[str, set[str]] = defaultdict(set)

    def info(path):
        resolved = path.resolve()
        if resolved not in info_cache:
            info_cache[resolved] = elf_info(resolved)
        return info_cache[resolved]

    def stage(source):
        resolved = source.resolve()
        if any(resolved.is_relative_to(root) for root in provided):
            return source
        target = destination / resolved.name
        _copy_file(resolved, target)
        staged.add(target)
        if source.name != target.name:
            alias = destination / source.name
            if alias.exists() or alias.is_symlink():
                if alias.resolve() != target.resolve():
                    raise ValueError(f"Conflicting native SONAME: {alias.name}")
            else:
                alias.symlink_to(target.name)
            staged.add(alias)
        return target

    while pending:
        source = pending.popleft()
        resolved = source.resolve()
        if resolved in visited:
            continue
        visited.add(resolved)
        details = info(source)
        for soname in details.needed:
            required = requirements[soname]
            required.update(details.requires.get(soname, set()))
            previous = selected.get(soname)
            if previous is not None and required <= info(previous).provides:
                continue
            compatible = [candidate for candidate in candidates.get(soname, ())
                          if required <= info(candidate).provides]
            if not compatible:
                raise RuntimeError(f"Unresolved native dependency {soname} for {source.name}; required versions: {sorted(required)}")
            candidate = compatible[0]
            selected[soname] = candidate
            pending.append(candidate)
    # Resolve every version requirement before copying. A later wheel can need
    # a newer libstdc++ than the SDK; upgrade the selected provider as a whole.
    for candidate in selected.values():
        stage(candidate)
    return sorted(staged)


def copy_configuration(sdk_source: Path, target: Path, installed_root: Path) -> list[str]:
    """Relocate SDK configuration; never invent missing robot calibrations."""
    shutil.copytree(sdk_source / "gdk/config", target / "gdk/config", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"), symlinks=False)
    missing = []
    for relative in ("conf/deploy/develop", "data/parameters/sensor"):
        source = sdk_source / relative
        if source.is_dir():
            shutil.copytree(source, target / relative, dirs_exist_ok=True, symlinks=False)
        else:
            # The distributed x86 remote-client SDK has no on-robot camera
            # deployment/calibration trees. Keep that absence explicit.
            missing.append(relative)
    aliases = {str(sdk_source.resolve()), "/home/agi/app", "~/.cache/agibot/app"}
    for relative in ("gdk/config", "conf/deploy/develop", "data/parameters/sensor"):
        for path in (target / relative).rglob("*"):
            if path.is_file() and path.suffix.lower() in (".json", ".yml", ".yaml", ".xml", ".cfg", ".conf", ".ini"):
                original = path.read_text()
                text = original
                for alias in sorted(aliases, key=len, reverse=True):
                    text = text.replace(alias + "/", str(installed_root) + "/")
                text = text.replace('"/data/parameters/sensor/', '"' + str(installed_root / "data/parameters/sensor") + "/")
                if text != original:
                    path.write_text(text)
    return missing


def bundle_gdk_runtime(package: Path, sdk_source: Path, python_sites: Sequence[Path], *,
                       required_modules: Sequence[str] = DEFAULT_MODULES,
                       extra_native_dirs: Sequence[Path] = (), private_sysroot: Path | None = None,
                       install_prefix: Path = INSTALL_PREFIX) -> dict:
    """Stage files under package/runtime/robot; caller patches RPATHs afterwards."""
    package, sdk_source = Path(package), Path(sdk_source)
    python_sites = [Path(path) for path in python_sites]
    private_sysroot = Path(private_sysroot) if private_sysroot else package / "runtime/sysroot"
    sdk_python = sdk_source / "gdk/lib"
    sdk_native = sdk_source / "lib"
    sdk_config = sdk_source / "gdk/config"
    extension = sdk_python / "agibot_gdk/agibot_gdk.cpython-310-x86_64-linux-gnu.so"
    if not extension.is_file() or not sdk_native.is_dir() or not sdk_config.is_dir():
        raise FileNotFoundError("G2 SDK requires gdk/lib/agibot_gdk (CPython 3.10), gdk/config and lib")
    for name in DYNAMIC_PLUGINS:
        if not is_elf(sdk_native / name):
            raise FileNotFoundError(f"G2 SDK dynamic communication plugin is missing or invalid: {name}")
    selected = select_distributions(python_sites, required_modules)
    target = package / "runtime/robot/gdk"
    site = package / "runtime/robot/python-site"
    if any(not path.resolve().is_relative_to(package.resolve()) for path in (target, site)):
        raise ValueError("Robot runtime destination escapes the package")
    shutil.copytree(sdk_python, target / "gdk/lib", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"), symlinks=False)
    missing_configuration = copy_configuration(sdk_source, target, Path(install_prefix) / "runtime/robot/gdk")
    for name in ("version",):
        if (sdk_source / "gdk" / name).is_file():
            _copy_file(sdk_source / "gdk" / name, target / "gdk" / name)
    if (sdk_native / "components.json").is_file():
        _copy_file(sdk_native / "components.json", target / "lib/components.json")
    python_binaries = copy_distributions(selected, site)
    sdk_binaries = [path for path in (target / "gdk/lib").rglob("*.so*") if is_elf(path)]
    for name in DYNAMIC_PLUGINS:
        for plugin in sorted(sdk_native.glob(name + "*")):
            if plugin.is_file():
                _copy_file(plugin, target / "lib" / plugin.name)
                sdk_binaries.append(target / "lib" / plugin.name)
    roots = [site, target / "gdk/lib", target / "lib", sdk_native, private_sysroot,
             *(Path(path) for path in extra_native_dirs)]
    native = copy_native_closure([*sdk_binaries, *python_binaries], target / "lib", roots,
                                 [package / "runtime/robot", private_sysroot])
    report = {
        "python_abi": "cpython-310-x86_64-linux-gnu",
        "dynamic_plugins": [str((target / "lib" / name).relative_to(package)) for name in DYNAMIC_PLUGINS],
        "python_modules": list(required_modules),
        "python_distributions": {name: dist.version for name, dist in sorted(selected.items())},
        "native_libraries": [str(path.relative_to(package)) for path in native],
        "python_elf_files": [str(path.relative_to(package)) for path in [*sdk_binaries, *python_binaries]],
        "optional_missing_configuration_dirs": missing_configuration,
    }
    (target / "runtime-manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    return report
