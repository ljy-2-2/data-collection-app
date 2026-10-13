#!/bin/sh
# Sole mutation outside the diagnostic directory: stop/start our own snapshot job.
set -eu
cd /tmp/linux-3.14.18-ipipe-build/fusion-three-mjpeg30b-20260920
test "$(uname -r)" = 3.14.18-ipipe
test -x ./capture-three
status fusion-camera | grep 'start/running' >&2
if pgrep -x capture >/dev/null || pgrep -x capture-three >/dev/null; then
  echo 'Existing camera capture is active; not starting' >&2
  exit 2
fi
mkdir audit
snapshot() {
  tag=$1
  cat /proc/xenomai/stat > "audit/$tag.xenomai"
  cat /proc/modules > "audit/$tag.modules"
  ps -eo pid,comm,ni,psr > "audit/$tag.processes"
  dmesg > "audit/$tag.dmesg"
  cat /proc/mounts > "audit/$tag.mounts"
  sha256sum /etc/init.d/FXStart.sh /home/FUSION/Shell/run.sh /home/FUSION/Shell/start.sh /boot/config-3.14.18-ipipe /etc/network/interfaces /etc/fstab /etc/rc.local > "audit/$tag.config-sha256"
}
snapshot before
getconf CLK_TCK > audit/clock-ticks
stopped_service=0
monitor_pid=''
cleanup() {
  result=$?
  trap - EXIT HUP INT TERM
  if test -n "$monitor_pid"; then
    kill "$monitor_pid" 2>/dev/null || true
    wait "$monitor_pid" 2>/dev/null || true
  fi
  snapshot after || true
  if test "$stopped_service" = 1; then
    if ! start fusion-camera >&2; then
      echo 'Snapshot service restoration failed' >&2
      result=3
    fi
  fi
  status fusion-camera >&2 || true
  echo "$result" > audit/wrapper-exit
  exit "$result"
}
trap cleanup EXIT
trap 'exit 143' HUP INT TERM
stop fusion-camera >&2
stopped_service=1
for p in 1-1 1-2 1-4; do
  for n in 0 1; do
    test ! -L "/sys/bus/usb/devices/$p:1.$n/driver"
    grep -q '^ *0*$' "/sys/bus/usb/devices/$p:1.$n/bAlternateSetting"
  done
done
if fuser /dev/bus/usb/001/002 /dev/bus/usb/001/003 /dev/bus/usb/001/004 >&2; then
  echo 'Camera USB node busy' >&2
  exit 2
fi
# Monitor in one bounded low-priority process. It does not issue USB or robot commands.
(
  renice 19 -p "$$" >/dev/null 2>&1 || true
  i=0
  while test "$i" -lt 40; do
    echo "SAMPLE $i"
    cat /proc/uptime /proc/stat /proc/meminfo /proc/net/dev /proc/xenomai/stat
    for proc_pid in $(pgrep -x sshd; pgrep -x capture-three); do
      if test -r "/proc/$proc_pid/stat"; then
        printf 'PROCESS_STAT '
        cat "/proc/$proc_pid/stat" 2>/dev/null || true
      fi
    done
    i=$((i+1))
    sleep 2
  done
) > audit/monitor.log &
monitor_pid=$!
# All status goes to stderr; stdout contains only the framed raw images.
timeout -k 3 75s nice -n 19 taskset -c 3 ./capture-three 60 \
  "$(cat /sys/bus/usb/devices/1-1/devnum)" \
  "$(cat /sys/bus/usb/devices/1-2/devnum)" \
  "$(cat /sys/bus/usb/devices/1-4/devnum)" mjpeg
