from __future__ import annotations

import os
import platform
import re
import subprocess
from pathlib import Path


def detect_robot_interface(
    configured: str, subnet_prefix: str = "10.42.1.", *, run_command=None
) -> str:
    """Use the recipient's uniquely matching robot NIC, or require a selection."""
    if configured and configured != "auto":
        return configured
    try:
        result = (run_command or subprocess.run)(["ip", "-o", "-4", "addr", "show"],
                                capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    interfaces = set()
    for row in result.stdout.splitlines() if result.returncode == 0 else []:
        match = re.search(r"^\s*\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/", row)
        if match and match.group(2).startswith(subnet_prefix):
            interfaces.add(match.group(1).split("@", 1)[0])
    return next(iter(interfaces)) if len(interfaces) == 1 else ""


def _prepend_env_path(env: dict[str, str], name: str, paths: list[Path]) -> None:
    current = [item for item in env.get(name, "").split(os.pathsep) if item]
    existing = [str(path) for path in paths if path.exists()]
    env[name] = os.pathsep.join(dict.fromkeys(existing + current))


def add_xr_runtime_env(env: dict[str, str], launcher_script: str) -> dict[str, str]:
    """Mirror the runtime paths exported by the vendor XR runService.sh."""
    from .runtime_paths import APP_ROOT

    root = Path(launcher_script).expanduser().resolve().parent
    arm = platform.machine().lower() in {"aarch64", "arm64"}
    sdk_dirs = [root / "SDK" / name for name in (("arm64", "aarch64") if arm else ("x64",))]
    # The ARM vendor service requires ICU 73, absent from Ubuntu 22.04. The
    # package carries these libraries privately; never replace system Qt/ICU.
    support_dirs = [APP_ROOT / ".runtime/xr-support", APP_ROOT.parent / "runtime/xr-support"]
    _prepend_env_path(env, "LD_LIBRARY_PATH", [*support_dirs, root, root / "lib", *sdk_dirs])
    # Keep this process on the vendor's matching Qt plugins, even when launched
    # from the desktop UI, which uses a different Qt version.
    env["QT_PLUGIN_PATH"] = str(root / "plugins")
    env["QT_QML_PATH"] = str(root / "qml")
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    return env


def refresh_discovery_env(env: dict[str, str], robot_interface: str = "auto") -> None:
    """Refresh G2 discovery settings after the robot Ethernet link changes."""
    env["TELEOP_ROBOT_INTERFACE"] = robot_interface or "auto"
    robot_interface = detect_robot_interface(robot_interface)
    addresses = ""
    if robot_interface:
        try:
            addresses = subprocess.check_output(
                ["ip", "-o", "-4", "addr", "show", "dev", robot_interface],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    env.pop("LOCATOR_IP", None)
    match = re.search(r"\binet (10\.42\.1\.\d+)/", addresses)
    if match:
        env["LOCATOR_IP"] = match.group(1)
    # This endpoint lives on the robot, not the collection PC. Set it even
    # before the robot NIC has an address so a later cable reconnect cannot leave GDK
    # silently falling back to 127.0.0.1 for the lifetime of the service.
    env["AORTA_DISCOVERY_URI"] = env.get(
        "TELEOP_AORTA_DISCOVERY_URI", "http://10.42.1.101:2379"
    )
    env["AORTA_DISPATCHER_THREAD_NUM"] = "6"


def build_gdk_env(
    app_root: str,
    base_env: dict[str, str] | None = None,
    *,
    robot_interface: str = "auto",
) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    root = Path(app_root).expanduser().resolve()
    python_dir = root / "gdk" / "lib"
    native_root = root / "lib"
    if not (python_dir / "agibot_gdk").is_dir():
        raise FileNotFoundError(f"agibot_gdk not found under {python_dir}")
    if not native_root.is_dir():
        raise FileNotFoundError(f"AgiBot native library directory not found: {native_root}")

    native_dirs = sorted({path.parent for path in native_root.rglob("*.so*") if path.is_file()})
    _prepend_env_path(env, "PYTHONPATH", [python_dir])
    _prepend_env_path(env, "LD_LIBRARY_PATH", native_dirs)
    # Prefer the vendor's documented YAML config, but use the bootstrap JSON
    # shipped by the x86 bundle when that generated YAML is absent.
    vendor_yaml = root / "gdk" / "config" / "app_conf.yml"
    vendor_json = root / "gdk" / "config" / "app_conf.json"
    if vendor_yaml.is_file():
        env.setdefault("APP_CONF_PATH", str(vendor_yaml))
    elif vendor_json.is_file():
        env.setdefault("APP_CONF_PATH", str(vendor_json))

    refresh_discovery_env(env, robot_interface)
    return env
