"""Read one real G2 joint snapshot without issuing a robot command."""

import argparse
import importlib
import math
import sys
import time

from teleop.contracts.robot_state import RobotState
from teleop.robots.g2 import G2Adapter


def _settle_seconds(value: str) -> float:
    try:
        converted = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(converted) or converted < 0.0:
        raise argparse.ArgumentTypeError("must be finite and non-negative")
    return converted


def read_real_g2_state(*, gdk: object, settle_seconds: float) -> RobotState:
    if (
        isinstance(settle_seconds, bool)
        or not isinstance(settle_seconds, (int, float))
        or not math.isfinite(settle_seconds)
        or settle_seconds < 0.0
    ):
        raise ValueError("settle_seconds must be finite and non-negative")

    success = gdk.GDKRes.kSuccess  # type: ignore[attr-defined]
    if gdk.gdk_init() != success:  # type: ignore[attr-defined]
        raise RuntimeError("GDK initialization failed; no robot reader was created")

    primary_error: BaseException | None = None
    try:
        robot = gdk.Robot()  # type: ignore[attr-defined]
        if settle_seconds:
            time.sleep(settle_seconds)
        adapter = G2Adapter(dry_run=False, state_reader=robot)
        state = adapter.read_state()
        adapter.stop()
        return state
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            release_result = gdk.gdk_release()  # type: ignore[attr-defined]
        except BaseException as release_error:
            if primary_error is None:
                raise RuntimeError("GDK release failed with an exception") from release_error
            print(
                f"WARNING: GDK release also failed while handling {primary_error!r}: "
                f"{release_error!r}",
                file=sys.stderr,
            )
        else:
            if release_result != success:
                if primary_error is None:
                    raise RuntimeError(f"GDK release failed: {release_result!r}")
                print(
                    f"WARNING: GDK release returned {release_result!r} while handling "
                    f"{primary_error!r}",
                    file=sys.stderr,
                )


def format_state(state: RobotState) -> str:
    joints = " ".join(
        f"{name}={position:+.6f}"
        for name, position in state.positions.items()
    )
    return (
        f"G2_READONLY_STATE joints={len(state.positions)} "
        f"received_monotonic_ns={state.timestamp_ns} {joints} "
        "physical_robot_command_sent=False"
    )


def _print_precheck() -> None:
    print(
        "G2_READONLY_PRECHECK mode=live-read-only "
        "physical_robot_command_sent=False",
        flush=True,
    )


def run(*, settle_seconds: float, emit_precheck: bool = True) -> int:
    if emit_precheck:
        _print_precheck()
    try:
        gdk = importlib.import_module("agibot_gdk")
        state = read_real_g2_state(gdk=gdk, settle_seconds=settle_seconds)
    except Exception as error:
        print(
            f"G2_READONLY_ERROR: {error} physical_robot_command_sent=False",
            file=sys.stderr,
            flush=True,
        )
        return 2
    print(format_state(state), flush=True)
    return 0


def main() -> int:
    _print_precheck()
    parser = argparse.ArgumentParser(
        description="Read all configured G2 joints without sending a robot command."
    )
    parser.add_argument(
        "--settle-seconds",
        type=_settle_seconds,
        default=2.0,
        help="wait after GDK initialization before the read (default: 2)",
    )
    args = parser.parse_args()
    return run(settle_seconds=args.settle_seconds, emit_precheck=False)


if __name__ == "__main__":
    raise SystemExit(main())
