#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PICO_CONNECTOR="${PICO_CONNECTOR:-$HERE/auto_connect_pico.sh}"
DUAL_ARM_PIPELINE_LAUNCHER="${DUAL_ARM_PIPELINE_LAUNCHER:-$HERE/run_pico_g2_preview.sh}"
enable_motion=false
dry_run=false
control_hz=100
auto_pico=true
head_follow=false
head_option=""
forwarded=()

usage() {
  cat <<'EOF'
Usage: bash apps/run_pico_g2_dual_arm_live.sh (--dry-run | --enable-motion) [options]

Options:
  --dry-run           Run the existing read-only dual-arm preview
  --enable-motion     Explicitly arm G2 dual-arm servo commands
  --duration SECONDS  Session duration
  --hz HZ             PICO/control rate (default: 100; minimum at 0.5 rad/s: 25)
  --no-auto-pico      Skip one-shot PICO setup when a parent supervisor owns it
  --head-follow      Add head following (requires live mode and 100 Hz)
  --no-head-follow   Disable head following (default)
  -h, --help          Show this help
EOF
}

while (($#)); do
  case "$1" in
    --enable-motion)
      enable_motion=true
      shift
      ;;
    --dry-run)
      dry_run=true
      shift
      ;;
    --hz)
      control_hz="${2:?missing value for --hz}"
      forwarded+=("$1" "$2")
      shift 2
      ;;
    --no-auto-pico)
      auto_pico=false
      shift
      ;;
    --head-follow|--no-head-follow)
      [[ -z "$head_option" || "$head_option" == "$1" ]] || { echo "Conflicting head options" >&2; exit 2; }
      head_option="$1"; [[ "$1" == --head-follow ]] && head_follow=true || head_follow=false
      shift ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      forwarded+=("$1")
      shift
      ;;
  esac
done

if [[ "$enable_motion" == true && "$dry_run" == true ]]; then
  echo "PICO_G2_DUAL_ARM_LIVE_REFUSED: choose only one of --dry-run or --enable-motion" >&2
  exit 2
fi
if [[ "$head_follow" == true ]]; then
  [[ "$enable_motion" == true && "$dry_run" == false && "$control_hz" =~ ^100([.]0+)?$ ]] || {
    echo "Head following requires live dual-arm at 100 Hz" >&2; exit 2;
  }
fi
if [[ "$dry_run" == true ]]; then
  exec bash "$HERE/run_pico_g2_dual_arm_preview.sh" "${forwarded[@]}"
fi
if [[ "$enable_motion" != true ]]; then
  echo "PICO_G2_DUAL_ARM_LIVE_REFUSED: choose --dry-run or --enable-motion" >&2
  exit 2
fi

if [[ "$auto_pico" == true ]]; then
  [[ -f "$PICO_CONNECTOR" ]] || {
    echo "PICO connector not found: $PICO_CONNECTOR" >&2
    exit 2
  }
  # Restore TCP 63901/63902 before any G2 command-capable process is started.
  bash "$PICO_CONNECTOR" --once
fi
[[ -f "$DUAL_ARM_PIPELINE_LAUNCHER" ]] || {
  echo "Dual-arm pipeline launcher not found: $DUAL_ARM_PIPELINE_LAUNCHER" >&2
  exit 2
}

export PICO_G2_PIPELINE_MODE="live-dual-arm"
export PICO_G2_PHYSICAL_MARKER="True"
export PICO_EMITTER="$HERE/pico_g2_dual_arm_live.py"
export G2_WORKER="$HERE/run_pico_g2_dual_arm_live_worker.sh"
if [[ "$head_follow" == true ]]; then
  # The selected script path survives sudo environment sanitization.
  export G2_WORKER="$HERE/run_pico_g2_dual_arm_head_live_worker.sh"
fi
export PICO_PY="${PICO_PY:-${HOME}/miniforge3/envs/xrobotoolkit-pybind/bin/python}"
export G2_DUAL_ARM_CONTROL_HZ="$control_hz"
export G2_DUAL_ARM_MAX_VELOCITY_RAD_S="0.5"

echo "PICO_G2_DUAL_ARM_LIVE_ARMED max_velocity_rad_s=0.5 control_hz=$control_hz"
echo "Safety: hold the matching Grip to move; release Grip to stop that arm."
echo "With both Grips released, hold B for one second to stop; Ctrl-C also stops."

exec bash "$DUAL_ARM_PIPELINE_LAUNCHER" "${forwarded[@]}"
