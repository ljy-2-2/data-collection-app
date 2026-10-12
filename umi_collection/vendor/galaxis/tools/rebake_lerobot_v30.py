#!/usr/bin/env python3
"""Convert Galaxis rosbags into profile-safe LeRobot Dataset v3.0 trees."""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from rebake_collection_profiles import discover_bags, write_profile_configs


PROJECT_DIR = Path(__file__).resolve().parents[1]
REBAKE_DIR = PROJECT_DIR / "rebake"
REBAKE_COMPOSE = REBAKE_DIR / "docker" / "docker-compose.yml"
BASE_PIPELINE = REBAKE_DIR / "config" / "pipeline" / "galaxis_v30_local.yaml"
BASE_MODEL = REBAKE_DIR / "config" / "robot_model" / "galaxis.yaml"
DATA_ROOT = Path(os.environ.get("UMI_DATA_ROOT", str(Path.home() / "galaxis_data"))).expanduser()
ROSBAG_ROOT = DATA_ROOT / "rosbags"
OUTPUT_ROOT = DATA_ROOT / "lerobot_v21"
TASK_OUTPUT_ROOT = DATA_ROOT / "lerobot_v30_task"


def operator_identity() -> tuple[str, str]:
    """Return the real operator identity, including when launched via sudo."""
    return (
        os.environ.get("SUDO_UID", str(os.getuid())),
        os.environ.get("SUDO_GID", str(os.getgid())),
    )


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    combined = os.environ.copy()
    if env:
        combined.update(env)
    print("+", " ".join(command), flush=True)
    result = subprocess.run(command, cwd=cwd, env=combined, check=False)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}")


def normalize_generated_paths(paths: list[Path], uid: str, gid: str) -> None:
    """Make generated trees operator-owned and recursively mode 777."""
    numeric_uid, numeric_gid = int(uid), int(gid)
    for base in paths:
        if not base.exists() or base.is_symlink():
            continue
        for root, dirs, files in os.walk(base, followlinks=False):
            candidates = [Path(root), *(Path(root) / name for name in dirs + files)]
            for path in candidates:
                if path.is_symlink():
                    continue
                if os.geteuid() == 0:
                    os.chown(path, numeric_uid, numeric_gid)
                os.chmod(path, 0o777)


def container_rosbag_arguments(bags: list[Path]) -> str:
    """Return direct container paths; Rebake does not follow directory symlinks."""
    return " ".join(
        shlex.quote(f"/data/rosbags/{bag.name}")
        for bag in bags
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Galaxis ROS bags -> profile-partitioned Rebake -> LeRobot v3.0"
    )
    parser.add_argument("--jobs", type=int, default=1, help="Parallel rosbag jobs")
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Only classify bags and print their destination profiles",
    )
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if not ROSBAG_ROOT.is_dir():
        raise RuntimeError(f"rosbag directory does not exist: {ROSBAG_ROOT}")

    grouped = discover_bags(ROSBAG_ROOT)
    nonempty = {profile: bags for profile, bags in grouped.items() if bags}
    if not nonempty:
        raise RuntimeError(f"no rosbag2 recordings found below {ROSBAG_ROOT}")
    for profile, bags in nonempty.items():
        print(f"{profile}: {len(bags)} bag(s)")
        for bag in bags:
            print(f"  - {bag.name}")
    if args.plan:
        return 0

    uid, gid = operator_identity()
    with tempfile.TemporaryDirectory(prefix="galaxis-rebake-") as runtime_text:
        runtime_root = Path(runtime_text)
        config_root = runtime_root / "configs"
        log_root = runtime_root / "logs"
        config_root.mkdir()
        log_root.mkdir()
        for profile in nonempty:
            write_profile_configs(
                BASE_MODEL,
                BASE_PIPELINE,
                config_root,
                profile,
                "/data/lerobot_v21",
                container_work_root="/runtime/work",
                container_config_root="/runtime/configs",
            )
        normalize_generated_paths([runtime_root], uid, gid)

        compose_env = {
            "DATA_MOUNT_PATH": str(DATA_ROOT),
            "REBAKE_RUNTIME_PATH": str(runtime_root),
            "GALAXIS_DATA_UID": uid,
            "GALAXIS_DATA_GID": gid,
        }
        run(
            ["docker", "compose", "-f", str(REBAKE_COMPOSE), "up", "-d", "--build", "rebake-dev"],
            cwd=REBAKE_DIR,
            env=compose_env,
        )
        # Building writes only to the named Cargo volume and remains root-owned.
        # All non-output artifacts live below /runtime and are deleted when
        # this context exits. Only v2.1 and v3.0 stay below /data.
        run(
            [
                "docker", "compose", "-f", str(REBAKE_COMPOSE), "exec",
                "rebake-dev", "bash", "-lc",
                "cd /usr/src/rebake-rs && if [ ! -x ./target/release/rebake-cli ]; then just build; fi",
            ],
            cwd=REBAKE_DIR,
            env=compose_env,
        )
        try:
            for profile in nonempty:
                bag_arguments = container_rosbag_arguments(nonempty[profile])
                command = (
                    "cd /usr/src/rebake-rs && "
                    f"REBAKE_LOG_DIR=/runtime/logs/{profile} "
                    "RUST_LOG=info REBAKE_LOG_STDERR=1 "
                    f"./target/release/rebake-cli run {bag_arguments} "
                    f"-c /runtime/configs/{profile}/pipeline.yaml -j {args.jobs}"
                )
                run(
                    [
                        "docker", "compose", "-f", str(REBAKE_COMPOSE), "exec",
                        "--user", f"{uid}:{gid}", "-e", "HOME=/tmp",
                        "rebake-dev", "bash", "-lc", command,
                    ],
                    cwd=REBAKE_DIR,
                    env=compose_env,
                )
                run(
                    [
                        "docker", "compose", "-f", str(REBAKE_COMPOSE), "exec",
                        "--user", f"{uid}:{gid}", "-e", "HOME=/tmp", "rebake-dev",
                        "python3", "tools/repair_lerobot_v21_center.py",
                        "--src", f"/data/lerobot_v21/{profile}",
                    ],
                    cwd=REBAKE_DIR,
                    env=compose_env,
                )
                run(
                    [
                        "docker", "compose", "-f", str(REBAKE_COMPOSE), "exec",
                        "--user", f"{uid}:{gid}", "-e", "HOME=/tmp", "rebake-dev",
                        "python3", "tools/aggregate_lerobot_v30.py",
                        "--src", f"/data/lerobot_v21/{profile}",
                        "--out", f"/data/lerobot_v30_task/{profile}",
                        "--profile", profile,
                    ],
                    cwd=REBAKE_DIR,
                    env=compose_env,
                )
        finally:
            normalize_generated_paths(
                [OUTPUT_ROOT, TASK_OUTPUT_ROOT], uid, gid
            )

    print(f"Profile-partitioned LeRobot v3.0 datasets ready: {TASK_OUTPUT_ROOT}")
    for profile in nonempty:
        print(f"  {profile}: {TASK_OUTPUT_ROOT / profile}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
