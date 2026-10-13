#!/bin/sh
XIAOKAI_PACKAGE=/opt/xiaokai-lab-data-collection
export XIAOKAI_SYSROOT="$XIAOKAI_PACKAGE/runtime/sysroot"
export XIAOKAI_DOCKER_SERVICE=xiaokai-collection-docker.service
export XIAOKAI_SECRET_PYTHON="$XIAOKAI_SYSROOT/usr/bin/python3.10"
export PATH="$XIAOKAI_PACKAGE/runtime/bin:$PATH"
export DOCKER_HOST=unix:///run/xiaokai-collection/docker.sock
export GI_TYPELIB_PATH="$XIAOKAI_SYSROOT/usr/lib/x86_64-linux-gnu/girepository-1.0"
export LIBGL_DRIVERS_PATH="$XIAOKAI_SYSROOT/usr/lib/x86_64-linux-gnu/dri"
export LIBGL_ALWAYS_SOFTWARE="${LIBGL_ALWAYS_SOFTWARE:-0}"
export XKB_CONFIG_ROOT="$XIAOKAI_SYSROOT/usr/share/X11/xkb"
export FONTCONFIG_FILE="$XIAOKAI_PACKAGE/runtime/fonts.conf"
export XDG_DATA_DIRS="$XIAOKAI_SYSROOT/usr/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
export GSETTINGS_SCHEMA_DIR="$XIAOKAI_SYSROOT/usr/share/glib-2.0/schemas"
# Private binaries carry their own interpreter/RPATH. Never export private
# glibc into unrelated host programs or robot SDK subprocesses.
unset LD_LIBRARY_PATH LD_PRELOAD
