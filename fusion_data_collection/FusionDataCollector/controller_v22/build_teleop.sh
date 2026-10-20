#!/bin/sh
# Reproducible native build; compiling or --self-test never opens USB.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
sdk_dir=$(CDPATH= cd -- "$script_dir/../.." && pwd)
vendor_dir="$sdk_dir/FusionCameraSDK/vendor"
output=${1:-"$sdk_dir/FusionDataCollector/build/capture-three-teleop"}
mkdir -p -- "$(dirname -- "$output")"
"${CC:-gcc}" -std=gnu99 -O1 -Wall -Wextra -Werror=implicit-function-declaration -pthread -DFUSION_STRICT_STREAM_QUERY -DFUSION_FRAME_COMPLETION_HOOK \
  -I"$vendor_dir" -I"$vendor_dir/libuvc-0.0.6/include" \
  "$script_dir/capture_teleop.c" \
  "$vendor_dir/libuvc-0.0.6/src/ctrl.c" "$vendor_dir/libuvc-0.0.6/src/ctrl-gen.c" \
  "$vendor_dir/libuvc-0.0.6/src/device.c" "$vendor_dir/libuvc-0.0.6/src/diag.c" \
  "$vendor_dir/libuvc-0.0.6/src/frame.c" "$vendor_dir/libuvc-0.0.6/src/init.c" \
  "$vendor_dir/libuvc-0.0.6/src/stream.c" "$vendor_dir/libuvc-0.0.6/src/misc.c" \
  "${LIBUSB_LIBRARY:--l:libusb-1.0.so.0}" -lrt -o "$output"
"$output" --self-test
