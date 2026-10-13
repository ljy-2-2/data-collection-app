#!/usr/bin/env bash
set -euo pipefail

# -----------------------
# Variant resolution: CLI flag overrides ROBOT_VARIANT env var; default
# stationary (matches docker-compose / launch defaults). Setup intentionally
# opens with every available capture-capable V4L2 node, even when fewer than
# the target role count are currently connected.
# -----------------------
VARIANT="${ROBOT_VARIANT:-stationary}"
ESP_VID_OVERRIDE=""
ESP_PID_OVERRIDE=""
COLLECTION_PROFILE="auto"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --variant)
      [[ $# -ge 2 ]] || { echo "ERROR: --variant requires an argument"; exit 2; }
      VARIANT="$2"
      shift 2
      ;;
    --variant=*)
      VARIANT="${1#--variant=}"
      shift
      ;;
    --esp-vid)
      [[ $# -ge 2 ]] || { echo "ERROR: --esp-vid requires an argument"; exit 2; }
      ESP_VID_OVERRIDE="${2,,}"
      shift 2
      ;;
    --esp-vid=*)
      ESP_VID_OVERRIDE="${1#--esp-vid=}"
      ESP_VID_OVERRIDE="${ESP_VID_OVERRIDE,,}"
      shift
      ;;
    --esp-pid)
      [[ $# -ge 2 ]] || { echo "ERROR: --esp-pid requires an argument"; exit 2; }
      ESP_PID_OVERRIDE="${2,,}"
      shift 2
      ;;
    --esp-pid=*)
      ESP_PID_OVERRIDE="${1#--esp-pid=}"
      ESP_PID_OVERRIDE="${ESP_PID_OVERRIDE,,}"
      shift
      ;;
    --collection-profile)
      [[ $# -ge 2 ]] || { echo "ERROR: --collection-profile requires an argument"; exit 2; }
      COLLECTION_PROFILE="$2"
      shift 2
      ;;
    --collection-profile=*)
      COLLECTION_PROFILE="${1#--collection-profile=}"
      shift
      ;;
    -h|--help)
      cat <<'USAGE'
Usage: sudo -E ./tools/galaxis_udev_setup.sh [--variant stationary|portable]
       [--esp-vid HEX --esp-pid HEX]

Calibrates the gripper encoders and writes /etc/udev/rules.d/99-galaxis-devices.rules.

Variant selection (precedence: CLI flag > $ROBOT_VARIANT env var > stationary):
  stationary  expects 2 USB cameras (left, right)
  portable    Setup buttons select either one hand + center (2 cameras,
              1 actuator) or left + right + center (3 cameras, 2 actuators).
              Setup still opens with any number of index-0 V4L2 candidates;
              complete role selection is validated only at APPLY.

Encoder serial selection:
  By default, every USB serial port is probed and only a device outputting the
  Galaxis L/R encoder protocol is accepted. To restrict probing to a known
  USB VID/PID instead, pass both values, for example:
    --esp-vid 1a86 --esp-pid 7523   # CH340
    --esp-vid 10c4 --esp-pid ea60   # CP210x
USAGE
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1"; exit 2
      ;;
  esac
done

if [[ -n "$ESP_VID_OVERRIDE" || -n "$ESP_PID_OVERRIDE" ]]; then
  if [[ -z "$ESP_VID_OVERRIDE" || -z "$ESP_PID_OVERRIDE" ]]; then
    echo "ERROR: --esp-vid and --esp-pid must be provided together" >&2
    exit 2
  fi
  if [[ ! "$ESP_VID_OVERRIDE" =~ ^[0-9a-f]{4}$ || ! "$ESP_PID_OVERRIDE" =~ ^[0-9a-f]{4}$ ]]; then
    echo "ERROR: encoder VID and PID must each be exactly four hexadecimal digits" >&2
    exit 2
  fi
fi

case "$VARIANT" in
  stationary) EXPECTED_CAM_COUNT=2 ;;
  portable)   EXPECTED_CAM_COUNT=2 ;;
  *)
    echo "ERROR: unsupported variant: $VARIANT (expected: stationary, portable)"
    exit 2
    ;;
esac
case "$COLLECTION_PROFILE" in
  auto|dual|single-left|single-right) ;;
  *)
    echo "ERROR: unsupported collection profile: $COLLECTION_PROFILE" >&2
    exit 2
    ;;
esac
if [[ "$VARIANT" == "portable" ]]; then
  echo "Variant: portable (Setup will select 2-camera single-arm or 3-camera dual-arm)"
else
  echo "Variant: $VARIANT (expect $EXPECTED_CAM_COUNT cameras)"
fi

# -----------------------
# Pause the galaxis container while we rewrite udev rules, so it doesn't
# hold /dev/video* handles (camera open fails if the container is up) and
# doesn't race on device symlinks. Covers both systemd-managed runs
# (docker.galaxis.service) and manual `docker compose up` runs.
#
# We do NOT restart galaxis on exit; bring it back up yourself once setup is
# done (e.g. `docker compose up -d`, or `systemctl start docker.galaxis.service`).
# -----------------------
GALAXIS_SERVICE="docker.galaxis.service"
GALAXIS_CONTAINER="galaxis"

service_exists() {
  command -v systemctl >/dev/null 2>&1 \
    && systemctl list-unit-files --no-legend 2>/dev/null \
       | awk '{print $1}' | grep -qx "$GALAXIS_SERVICE"
}

container_is_running() {
  command -v docker >/dev/null 2>&1 || return 1
  [[ "$(docker inspect -f '{{.State.Running}}' "$GALAXIS_CONTAINER" 2>/dev/null)" == "true" ]]
}

if service_exists && systemctl is-active --quiet "$GALAXIS_SERVICE"; then
  echo "Stopping $GALAXIS_SERVICE for udev setup..."
  systemctl stop "$GALAXIS_SERVICE"
elif container_is_running; then
  echo "Stopping container $GALAXIS_CONTAINER for udev setup..."
  docker stop "$GALAXIS_CONTAINER" >/dev/null
fi

# -----------------------
# Camera role symlinks.  The operator chooses candidates in Setup, so do not
# pre-filter by vendor/product here: a hub replug can change enumeration and
# camera models can vary across the rig.
# -----------------------
CAM_VID="32e4"
CAM_PID="9230"
PORTABLE_CAM_USB_IDS=("32e4:9230" "32e6:9005")
CAM_LEFT_SYMLINK="galaxis_left_camera"
CAM_RIGHT_SYMLINK="galaxis_right_camera"
CAM_CENTER_SYMLINK="galaxis_center_camera"

# -----------------------
# ESP32-C6 USB JTAG/serial (MT6835 over SPI -> legacy USB serial protocol)
# -----------------------
ESP_VID="${ESP_VID_OVERRIDE:-303a}"
ESP_PID="${ESP_PID_OVERRIDE:-1001}"
ESP_AUTO=1
if [[ -n "$ESP_VID_OVERRIDE" ]]; then
  ESP_AUTO=0
fi
ESP_LEFT_SYMLINK="galaxis_left_esp32c6"
ESP_RIGHT_SYMLINK="galaxis_right_esp32c6"

# -----------------------
# Outputs
# -----------------------
CALIB_DIR="/etc/galaxis"
CALIB_FILE="$CALIB_DIR/encoder_limits.yaml"
CENTER_CAMERA_FILE="$CALIB_DIR/center_camera.yaml"
COLLECTION_PROFILE_FILE="$CALIB_DIR/collection_profile.yaml"
RULE_FILE="/etc/udev/rules.d/99-galaxis-devices.rules"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

require() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1"; exit 2; }; }
require udevadm
require python3

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo:"
  echo "  sudo -E $0"
  exit 1
fi

# If running under sudo from a desktop session, try to keep GUI working.
if [[ -n "${SUDO_USER:-}" ]]; then
  export DISPLAY="${DISPLAY:-:0}"
  if [[ -z "${XAUTHORITY:-}" ]]; then
    if [[ -f "/home/$SUDO_USER/.Xauthority" ]]; then
      export XAUTHORITY="/home/$SUDO_USER/.Xauthority"
    fi
  fi
fi

APT_UPDATED=0

apt_install() {
  local pkg="$1"
  if dpkg -s "$pkg" >/dev/null 2>&1; then
    return 0
  fi
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "ERROR: apt-get not available to install $pkg" >&2
    return 1
  fi
  if [[ $APT_UPDATED -eq 0 ]]; then
    apt-get update
    APT_UPDATED=1
  fi
  DEBIAN_FRONTEND=noninteractive apt-get install -y "$pkg"
}

ensure_pip() {
  if python3 -m pip --version >/dev/null 2>&1; then
    return 0
  fi
  if command -v apt-get >/dev/null 2>&1; then
    echo "Installing python3-pip via apt..."
    apt_install python3-pip
    return 0
  fi
  echo "ERROR: pip is required but could not be installed automatically." >&2
  exit 2
}

ensure_python_dep() {
  local module="$1"
  local apt_pkg="${2:-}"
  local pip_pkg="${3:-$1}"
  if python3 -c "import $module" >/dev/null 2>&1; then
    return 0
  fi
  echo "Python module '$module' missing; attempting automatic installation..."
  if [[ -n "$apt_pkg" ]] && command -v apt-get >/dev/null 2>&1; then
    if apt_install "$apt_pkg"; then
      if python3 -c "import $module" >/dev/null 2>&1; then
        return 0
      fi
    fi
  fi
  ensure_pip
  if python3 -m pip install -U "$pip_pkg"; then
    if python3 -c "import $module" >/dev/null 2>&1; then
      return 0
    fi
  fi
  echo "ERROR: Failed to install Python module '$module'." >&2
  exit 2
}

# GUI deps
ensure_python_dep "tkinter" "python3-tk" "tk"
ensure_python_dep "serial" "python3-serial" "pyserial"
ensure_python_dep "cv2" "python3-opencv" "opencv-python"
ensure_python_dep "PIL.Image" "python3-pil" "pillow"
ensure_python_dep "PIL.ImageTk" "python3-pil.imagetk" "pillow"
ensure_python_dep "matplotlib" "python3-matplotlib" "matplotlib"
if ! command -v v4l2-ctl >/dev/null 2>&1; then
  apt_install v4l-utils
fi

get_prop() {
  local info="$1"
  local key="$2"
  grep -E "^${key}=" <<<"$info" | head -n1 | cut -d= -f2- || true
}

# -----------------------
# Camera detection: enumerate every capture interface (index==0) that has a
# stable udev path.  Setup presents these candidates and requires the operator
# to pick distinct roles; the chosen VID/PID + ID_PATH are what get persisted.
# -----------------------
cam_candidates=()
declare -A CAM_PATH_BY_DEV
declare -A CAM_VID_BY_DEV
declare -A CAM_PID_BY_DEV
for dev in /dev/video*; do
  [[ -e "$dev" ]] || continue
  idx_path="/sys/class/video4linux/$(basename "$dev")/index"
  [[ -f "$idx_path" ]] || continue
  idx="$(cat "$idx_path")"
  [[ "$idx" == "0" ]] || continue

  info="$(udevadm info --query=property --name="$dev" 2>/dev/null || true)"
  v="$(get_prop "$info" "ID_VENDOR_ID")"
  p="$(get_prop "$info" "ID_MODEL_ID")"
  id_path="$(get_prop "$info" "ID_PATH")"
  if [[ -z "$id_path" ]]; then
    id_path="$(get_prop "$info" "ID_PATH_TAG")"
  fi
  [[ -n "$id_path" ]] || { echo "WARN: $dev has no ID_PATH(_TAG); skip"; continue; }

  cam_candidates+=("$dev|$id_path")
  CAM_PATH_BY_DEV["$dev"]="$id_path"
  CAM_VID_BY_DEV["$dev"]="$v"
  CAM_PID_BY_DEV["$dev"]="$p"
done

if [[ ${#cam_candidates[@]} -lt $EXPECTED_CAM_COUNT ]]; then
  echo "WARN: variant=$VARIANT needs at least $EXPECTED_CAM_COUNT cameras, found ${#cam_candidates[@]}."
  echo "      Opening Setup anyway; select/complete the camera roles before APPLY."
fi

# -----------------------
# ESP32-C6 detection: native USB JTAG/serial devices with VID/PID = 303a:1001
# Use pyserial list_ports in the GUI script (more robust),
# but we also sanity-check count here by asking the GUI to validate.
# -----------------------

echo "Detected candidates:"
for candidate in "${cam_candidates[@]}"; do
  candidate_dev="${candidate%%|*}"
  candidate_path="${candidate#*|}"
  echo "  $candidate_dev  VID:PID=${CAM_VID_BY_DEV[$candidate_dev]}:${CAM_PID_BY_DEV[$candidate_dev]} ID_PATH=$candidate_path"
done
echo

# -----------------------
# Run unified GUI and get JSON result
# -----------------------
GUI="$SCRIPT_DIR/galaxis_device_select_gui.py"
if [[ ! -f "$GUI" ]]; then
  echo "ERROR: GUI script not found: $GUI"
  exit 4
fi

echo "Launching GUI..."
GUI_ARGS=(
  --cam-vid "$CAM_VID" --cam-pid "$CAM_PID"
  --esp-vid "$ESP_VID" --esp-pid "$ESP_PID"
  --baud 115200
  --variant "$VARIANT"
  --collection-profile "$COLLECTION_PROFILE"
)
if [[ "$ESP_AUTO" -eq 1 ]]; then
  GUI_ARGS+=(--esp-auto)
fi
if [[ -n "${GALAXIS_PICO_SERIAL:-}" ]]; then
  GUI_ARGS+=(--vr-serial "$GALAXIS_PICO_SERIAL")
fi
for candidate in "${cam_candidates[@]}"; do
  GUI_ARGS+=(--camera "${candidate%%|*}")
done
result_json="$(python3 "$GUI" "${GUI_ARGS[@]}")"

camera_field() {
  python3 -c '
import json, sys
camera = json.loads(sys.argv[1])["camera"].get(sys.argv[2], {})
print(camera.get(sys.argv[3], ""))
' "$result_json" "$1" "$2"
}

# Parse JSON in bash via python (robust)
collection_profile="$(python3 - <<PY
import json
print(json.loads('''$result_json''')["collection_profile"])
PY
)"
active_arms="$(python3 - <<PY
import json
print(" ".join(json.loads('''$result_json''')["active_arms"]))
PY
)"
active_cameras="$(python3 - <<PY
import json
print(" ".join(json.loads('''$result_json''')["active_cameras"]))
PY
)"
cam_left="$(python3 - <<PY
import json
print(json.loads('''$result_json''')["camera"].get("left", {}).get("dev", ""))
PY
)"
cam_right="$(python3 - <<PY
import json
print(json.loads('''$result_json''')["camera"].get("right", {}).get("dev", ""))
PY
)"
cam_center=""
center_source="usb"
center_vertical_flip="false"
center_brightness="0"
left_brightness="$(python3 - <<PY
import json
print(int(json.loads('''$result_json''')["camera"].get("left", {}).get("brightness", 0)))
PY
)"
right_brightness="$(python3 - <<PY
import json
print(int(json.loads('''$result_json''')["camera"].get("right", {}).get("brightness", 0)))
PY
)"
left_width="$(camera_field left image_width)"
left_height="$(camera_field left image_height)"
left_framerate="$(camera_field left framerate)"
left_pixel_format="$(camera_field left pixel_format)"
right_width="$(camera_field right image_width)"
right_height="$(camera_field right image_height)"
right_framerate="$(camera_field right framerate)"
right_pixel_format="$(camera_field right pixel_format)"
center_width=""
center_height=""
center_framerate=""
center_pixel_format=""
if [[ " $active_cameras " == *" center "* ]]; then
  center_source="$(python3 - <<PY
import json
print(json.loads('''$result_json''').get("camera_sources", {}).get("center", "usb"))
PY
)"
  cam_center="$(python3 - <<PY
import json
print(json.loads('''$result_json''')["camera"]["center"].get("dev", ""))
PY
)"
  center_vertical_flip="$(python3 - <<PY
import json
print(str(bool(json.loads('''$result_json''')["camera"]["center"].get("vertical_flip", False))).lower())
PY
)"
  center_brightness="$(python3 - <<PY
import json
print(int(json.loads('''$result_json''')["camera"]["center"].get("brightness", 0)))
PY
)"
  center_width="$(camera_field center image_width)"
  center_height="$(camera_field center image_height)"
  center_framerate="$(camera_field center framerate)"
  center_pixel_format="$(camera_field center pixel_format)"
fi

esp_left_serial="$(python3 - <<PY
import json
print(json.loads('''$result_json''')["esp32"].get("left", {}).get("serial", ""))
PY
)"
esp_right_serial="$(python3 - <<PY
import json
print(json.loads('''$result_json''')["esp32"].get("right", {}).get("serial", ""))
PY
)"
esp_left_dev="$(python3 - <<PY
import json
print(json.loads('''$result_json''')["esp32"].get("left", {}).get("dev", ""))
PY
)"
esp_right_dev="$(python3 - <<PY
import json
print(json.loads('''$result_json''')["esp32"].get("right", {}).get("dev", ""))
PY
)"

serial_prop() {
  local dev="$1"
  local key="$2"
  [[ -n "$dev" ]] || return 0
  local info
  info="$(udevadm info --query=property --name="$dev" 2>/dev/null || true)"
  get_prop "$info" "$key"
}

esp_left_path="$(serial_prop "$esp_left_dev" "ID_PATH")"
esp_right_path="$(serial_prop "$esp_right_dev" "ID_PATH")"
esp_left_vid="$(serial_prop "$esp_left_dev" "ID_VENDOR_ID")"
esp_left_pid="$(serial_prop "$esp_left_dev" "ID_MODEL_ID")"
esp_right_vid="$(serial_prop "$esp_right_dev" "ID_VENDOR_ID")"
esp_right_pid="$(serial_prop "$esp_right_dev" "ID_MODEL_ID")"

if [[ -n "$esp_left_dev" && -z "$esp_left_serial" && -z "$esp_left_path" ]]; then
  echo "ERROR: $esp_left_dev has neither a USB serial number nor ID_PATH; cannot create a stable left encoder rule." >&2
  exit 5
fi
if [[ -n "$esp_right_dev" && -z "$esp_right_serial" && -z "$esp_right_path" ]]; then
  echo "ERROR: $esp_right_dev has neither a USB serial number nor ID_PATH; cannot create a stable right encoder rule." >&2
  exit 5
fi

left_min="$(python3 - <<PY
import json
print(json.loads('''$result_json''')["esp32"].get("left", {}).get("min", ""))
PY
)"
right_min="$(python3 - <<PY
import json
print(json.loads('''$result_json''')["esp32"].get("right", {}).get("min", ""))
PY
)"


cam_left_path=""; cam_left_vid=""; cam_left_pid=""
cam_right_path=""; cam_right_vid=""; cam_right_pid=""
if [[ -n "$cam_left" ]]; then
  cam_left_path="${CAM_PATH_BY_DEV[$cam_left]}"; cam_left_vid="${CAM_VID_BY_DEV[$cam_left]}"; cam_left_pid="${CAM_PID_BY_DEV[$cam_left]}"
fi
if [[ -n "$cam_right" ]]; then
  cam_right_path="${CAM_PATH_BY_DEV[$cam_right]}"; cam_right_vid="${CAM_VID_BY_DEV[$cam_right]}"; cam_right_pid="${CAM_PID_BY_DEV[$cam_right]}"
fi
cam_center_path=""
cam_center_vid=""
cam_center_pid=""
if [[ -n "$cam_center" ]]; then
  cam_center_path="${CAM_PATH_BY_DEV[$cam_center]}"
  cam_center_vid="${CAM_VID_BY_DEV[$cam_center]}"
  cam_center_pid="${CAM_PID_BY_DEV[$cam_center]}"
fi

echo
echo "Assigned:"
echo "  Collection profile: $collection_profile"
[[ -n "$cam_left" ]] && echo "  Camera left  : $cam_left  VID:PID=$cam_left_vid:$cam_left_pid ID_PATH=$cam_left_path"
[[ -n "$cam_right" ]] && echo "  Camera right : $cam_right VID:PID=$cam_right_vid:$cam_right_pid ID_PATH=$cam_right_path"
if [[ "$center_source" == "g2_head_color" ]]; then
  echo "  Camera center: G2 head color camera via GDK"
elif [[ -n "$cam_center" ]]; then
  echo "  Camera center: $cam_center VID:PID=$cam_center_vid:$cam_center_pid ID_PATH=$cam_center_path"
fi
[[ -n "$esp_left_dev" ]] && echo "  ESP32 left   : dev=$esp_left_dev serial=${esp_left_serial:-none} path=${esp_left_path:-none} min=$left_min"
[[ -n "$esp_right_dev" ]] && echo "  ESP32 right  : dev=$esp_right_dev serial=${esp_right_serial:-none} path=${esp_right_path:-none} min=$right_min"
echo

# -----------------------
# Write ROS param yaml (example structure)
# -----------------------
mkdir -p "$CALIB_DIR"
timestamp="$(date --iso-8601=seconds)"

cat > "$CALIB_FILE" <<EOF
# Auto-generated by galaxis_udev_setup.sh on $timestamp
encoder_node:
  ros__parameters:
    active_sides: [$(sed 's/ /, /g' <<<"$active_arms")]
EOF
[[ -n "$left_min" ]] && echo "    left_min_raw: $left_min" >> "$CALIB_FILE"
[[ -n "$right_min" ]] && echo "    right_min_raw: $right_min" >> "$CALIB_FILE"

chmod 644 "$CALIB_FILE"
echo "Wrote: $CALIB_FILE"

if [[ -n "$cam_left$cam_right$cam_center" ]]; then
  cat > "$CENTER_CAMERA_FILE" <<EOF
# Auto-generated by galaxis_udev_setup.sh on $timestamp
left_camera_brightness_controller:
  ros__parameters:
    brightness: $left_brightness
right_camera_brightness_controller:
  ros__parameters:
    brightness: $right_brightness
center_camera_flip:
  ros__parameters:
    vertical_flip: $center_vertical_flip
center_camera_brightness_controller:
  ros__parameters:
    brightness: $center_brightness
EOF
  chmod 644 "$CENTER_CAMERA_FILE"
  echo "Wrote: $CENTER_CAMERA_FILE (hardware brightness: left=$left_brightness right=$right_brightness center=$center_brightness)"
fi

cat > "$COLLECTION_PROFILE_FILE" <<EOF
# Auto-generated by galaxis_udev_setup.sh on $timestamp
collection_profile: $collection_profile
active_cameras: [$(sed 's/ /, /g' <<<"$active_cameras")]
active_arms: [$(sed 's/ /, /g' <<<"$active_arms")]
camera_sources:
  center: "$center_source"
cameras:
EOF
if [[ -n "$cam_left" ]]; then
  cat >> "$COLLECTION_PROFILE_FILE" <<EOF
  left:
    image_width: $left_width
    image_height: $left_height
    framerate: $left_framerate
    pixel_format: "$left_pixel_format"
EOF
fi
if [[ -n "$cam_right" ]]; then
  cat >> "$COLLECTION_PROFILE_FILE" <<EOF
  right:
    image_width: $right_width
    image_height: $right_height
    framerate: $right_framerate
    pixel_format: "$right_pixel_format"
EOF
fi
if [[ " $active_cameras " == *" center "* ]]; then
  cat >> "$COLLECTION_PROFILE_FILE" <<EOF
  center:
    source: "$center_source"
    image_width: $center_width
    image_height: $center_height
    framerate: $center_framerate
    pixel_format: "$center_pixel_format"
EOF
fi
chmod 644 "$COLLECTION_PROFILE_FILE"
echo "Wrote: $COLLECTION_PROFILE_FILE"


# -----------------------
# Write udev rules directly into /etc/udev/rules.d
# -----------------------
if [[ -f "$RULE_FILE" ]]; then
  cp -a "$RULE_FILE" "${RULE_FILE}.$(date +%Y%m%d_%H%M%S).bak"
fi

cat > "$RULE_FILE" <<EOF
# Auto-generated by galaxis_udev_setup.sh (variant=$VARIANT)
# Cameras (UVC): role-specific VID/PID + stable USB ID_PATH, index=0
EOF
if [[ -n "$cam_left_path" ]]; then
  echo "SUBSYSTEM==\"video4linux\", KERNEL==\"video*\", ATTRS{idVendor}==\"$cam_left_vid\", ATTRS{idProduct}==\"$cam_left_pid\", ATTR{index}==\"0\", ENV{ID_PATH}==\"$cam_left_path\", SYMLINK+=\"$CAM_LEFT_SYMLINK\"" >> "$RULE_FILE"
fi
if [[ -n "$cam_right_path" ]]; then
  echo "SUBSYSTEM==\"video4linux\", KERNEL==\"video*\", ATTRS{idVendor}==\"$cam_right_vid\", ATTRS{idProduct}==\"$cam_right_pid\", ATTR{index}==\"0\", ENV{ID_PATH}==\"$cam_right_path\", SYMLINK+=\"$CAM_RIGHT_SYMLINK\"" >> "$RULE_FILE"
fi

if [[ -n "$cam_center_path" ]]; then
  cat >> "$RULE_FILE" <<EOF
SUBSYSTEM=="video4linux", KERNEL=="video*", ATTRS{idVendor}=="$cam_center_vid", ATTRS{idProduct}=="$cam_center_pid", ATTR{index}=="0", ENV{ID_PATH}=="$cam_center_path", SYMLINK+="$CAM_CENTER_SYMLINK"
EOF
fi

cat >> "$RULE_FILE" <<EOF

# ESP32-C6 + MT6835 serial stream: $ESP_VID:$ESP_PID
# Use serial to distinguish; also ignore ModemManager
EOF
if [[ -n "$esp_left_dev" ]]; then
  if [[ -n "$esp_left_serial" ]]; then
    echo "SUBSYSTEM==\"tty\", ENV{ID_VENDOR_ID}==\"$esp_left_vid\", ENV{ID_MODEL_ID}==\"$esp_left_pid\", ENV{ID_SERIAL_SHORT}==\"$esp_left_serial\", SYMLINK+=\"$ESP_LEFT_SYMLINK\", ENV{ID_MM_DEVICE_IGNORE}=\"1\", ENV{ID_MM_PORT_IGNORE}=\"1\"" >> "$RULE_FILE"
  else
    echo "SUBSYSTEM==\"tty\", ENV{ID_VENDOR_ID}==\"$esp_left_vid\", ENV{ID_MODEL_ID}==\"$esp_left_pid\", ENV{ID_PATH}==\"$esp_left_path\", SYMLINK+=\"$ESP_LEFT_SYMLINK\", ENV{ID_MM_DEVICE_IGNORE}=\"1\", ENV{ID_MM_PORT_IGNORE}=\"1\"" >> "$RULE_FILE"
  fi
fi
if [[ -n "$esp_right_dev" ]]; then
  if [[ -n "$esp_right_serial" ]]; then
    echo "SUBSYSTEM==\"tty\", ENV{ID_VENDOR_ID}==\"$esp_right_vid\", ENV{ID_MODEL_ID}==\"$esp_right_pid\", ENV{ID_SERIAL_SHORT}==\"$esp_right_serial\", SYMLINK+=\"$ESP_RIGHT_SYMLINK\", ENV{ID_MM_DEVICE_IGNORE}=\"1\", ENV{ID_MM_PORT_IGNORE}=\"1\"" >> "$RULE_FILE"
  else
    echo "SUBSYSTEM==\"tty\", ENV{ID_VENDOR_ID}==\"$esp_right_vid\", ENV{ID_MODEL_ID}==\"$esp_right_pid\", ENV{ID_PATH}==\"$esp_right_path\", SYMLINK+=\"$ESP_RIGHT_SYMLINK\", ENV{ID_MM_DEVICE_IGNORE}=\"1\", ENV{ID_MM_PORT_IGNORE}=\"1\"" >> "$RULE_FILE"
  fi
fi

chmod 644 "$RULE_FILE"
echo "Wrote: $RULE_FILE"

echo "Reloading udev rules..."
# Remove only inactive role links from the prior profile. They are regenerated
# by udev below when active; leaving an old link would let ROS open an excluded
# device and would make profile validation misleading.
[[ -z "$cam_left" && -L "/dev/$CAM_LEFT_SYMLINK" ]] && rm -f -- "/dev/$CAM_LEFT_SYMLINK"
[[ -z "$cam_right" && -L "/dev/$CAM_RIGHT_SYMLINK" ]] && rm -f -- "/dev/$CAM_RIGHT_SYMLINK"
[[ -z "$cam_center" && -L "/dev/$CAM_CENTER_SYMLINK" ]] && rm -f -- "/dev/$CAM_CENTER_SYMLINK"
[[ -z "$esp_left_dev" && -L "/dev/$ESP_LEFT_SYMLINK" ]] && rm -f -- "/dev/$ESP_LEFT_SYMLINK"
[[ -z "$esp_right_dev" && -L "/dev/$ESP_RIGHT_SYMLINK" ]] && rm -f -- "/dev/$ESP_RIGHT_SYMLINK"
for stale_path in \
  "/dev/$CAM_LEFT_SYMLINK" \
  "/dev/$CAM_RIGHT_SYMLINK" \
  "/dev/$CAM_CENTER_SYMLINK"; do
  if [[ -e "$stale_path" && ! -L "$stale_path" ]]; then
    echo "Removing stale non-symlink placeholder: $stale_path"
    rm -f -- "$stale_path"
  fi
done
udevadm control --reload-rules
udevadm trigger
sleep 0.2

echo "Result:"
ls -l /dev/galaxis_* || true
echo "Done."
