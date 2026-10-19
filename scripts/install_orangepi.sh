#!/usr/bin/env bash
# Native ARM64 build/install on Orange Pi Ubuntu 22.04. Run as the desktop user.
set -euo pipefail
OPI_APP_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ $EUID -eq 0 ]]; then
  echo '请以实际使用软件的普通用户运行，不要在整个脚本前添加 sudo。' >&2
  exit 1
fi
if [[ $(uname -m) != aarch64 ]]; then
  echo '此安装入口仅用于 ARM64 开发板；电脑版本继续使用 build-deb.sh。' >&2
  exit 1
fi
source /etc/os-release
if [[ ${ID:-} != ubuntu || ${VERSION_ID:-} != 22.04 ]]; then
  echo '此入口针对已选定的 Orange Pi Ubuntu 22.04 镜像，其他系统需另行验证。' >&2
  exit 1
fi
# Orange Pi Jammy may ship a docker.list without its signing key. This app
# uses Ubuntu's docker.io, so scope apt to the signed distribution sources;
# leave the owner's third-party repository configuration untouched.
OPI_APT_OPTIONS=(-o Dir::Etc::sourcelist=/etc/apt/sources.list -o Dir::Etc::sourceparts=-)
OPI_APK=${1:-"$OPI_APP_ROOT/umi_collection/headset/galaxis-vr.apk"}
if [[ ! -f "$OPI_APK" ]]; then
  echo '缺少 Galaxis PICO APK。用法：bash scripts/install_orangepi.sh /路径/Galaxis-VR.apk' >&2
  exit 1
fi
python3 - "$OPI_APP_ROOT" <<'PY'
import shutil,sys
if shutil.disk_usage(sys.argv[1]).free < 25 * 1024**3:
    raise SystemExit('构建需要至少 25 GiB 可用空间，请在 NVMe 上构建。')
PY
if [[ ${OPI_BUILD_STAGE:-} != ready ]]; then
  echo '安装 ARM64 构建、相机、容器和桌面依赖。首次构建需要联网，采集时不下载组件。'
  sudo --preserve-env=http_proxy,https_proxy,no_proxy apt-get "${OPI_APT_OPTIONS[@]}" update
  sudo --preserve-env=http_proxy,https_proxy,no_proxy apt-get "${OPI_APT_OPTIONS[@]}" install --no-upgrade -y build-essential cmake git patchelf python3-venv python3-pip python3-gi gir1.2-secret-1 gnome-keyring libpam-gnome-keyring dbus-user-session \
    docker.io docker-compose-v2 docker-buildx adb android-sdk-platform-tools-common v4l-utils \
    usbutils curl avahi-daemon udev policykit-1 zenity passwd fonts-noto-cjk \
    ca-certificates xdg-utils libegl1 libgl1 libgl1-mesa-dri libopengl0 \
    libwayland-client0 libwayland-cursor0 libwayland-egl1 libxcb-shm0 libxcb-util1 \
    libzstd1 libpng16-16 zlib1g libxkbcommon0 libxkbcommon-x11-0 libxcb-cursor0 \
    libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 \
    libxcb-xinerama0 libxcb-randr0 libxcb-shape0 libxcb-xfixes0 libxcb-sync1 \
    libxcb-xkb1 libxcb-render0 libxcb1 libx11-6 libx11-xcb1 libfontconfig1 \
    libfreetype6 libdbus-1-3 libsm6 libice6 libstdc++6 libgcc-s1 libglib2.0-0 libgomp1
  sudo systemctl start docker
  if ! docker info >/dev/null 2>&1; then
    echo '为当前采集用户配置 Docker 权限（该权限允许管理本机容器）。'
    sudo usermod -aG docker "$(id -un)"
    OPI_REEXEC=$(python3 - "$0" "$OPI_APK" <<'PY'
import shlex,sys
print(shlex.join(['env','OPI_BUILD_STAGE=ready','bash',sys.argv[1],sys.argv[2]]))
PY
)
    exec sg docker -c "$OPI_REEXEC"
  fi
fi
docker info >/dev/null
docker compose version
if ! docker buildx version >/dev/null 2>&1; then
  sudo --preserve-env=http_proxy,https_proxy,no_proxy apt-get "${OPI_APT_OPTIONS[@]}" install --no-upgrade -y docker-buildx
fi
# Keep authentication scoped to this installer terminal; no password is stored.
sudo -v
( while sleep 60; do sudo -n -v || exit; done ) &
OPI_SUDO_KEEPALIVE=$!
OPI_PROXY_FILE=/run/systemd/system/docker.service.d/90-orangepi-build-proxy.conf
OPI_PROXY_CREATED=0
opi_cleanup() {
  if [[ $OPI_PROXY_CREATED == 1 ]]; then
    sudo -n rm -f "$OPI_PROXY_FILE"
    sudo -n systemctl daemon-reload
    sudo -n systemctl restart docker
  fi
  kill "$OPI_SUDO_KEEPALIVE" 2>/dev/null || true
}
trap opi_cleanup EXIT
if [[ -n ${OPI_DOCKER_PROXY:-} ]]; then
  # This temporary override is removed on exit and never shipped in the app.
  if [[ $OPI_DOCKER_PROXY != http://127.0.0.1:17897 ]]; then
    echo '安装代理只允许本次 SSH 转发的回环地址。' >&2
    exit 1
  fi
  sudo mkdir -p "$(dirname "$OPI_PROXY_FILE")"
  printf '[Service]\nEnvironment="HTTP_PROXY=%s" "HTTPS_PROXY=%s"\n' "$OPI_DOCKER_PROXY" "$OPI_DOCKER_PROXY" | sudo tee "$OPI_PROXY_FILE" >/dev/null
  OPI_PROXY_CREATED=1
  sudo systemctl daemon-reload
  sudo systemctl restart docker
fi
mkdir -p "$OPI_APP_ROOT/.build-cache"
python3 -m venv "$OPI_APP_ROOT/.build-cache/bootstrap"
"$OPI_APP_ROOT/.build-cache/bootstrap/bin/python" -m pip install 'uv==0.8.22'
export PATH="$OPI_APP_ROOT/.build-cache/bootstrap/bin:$PATH"
uv python install 3.12 --no-bin --install-dir "$OPI_APP_ROOT/.build-cache/python" --cache-dir "$OPI_APP_ROOT/.build-cache/uv"
OPI_PYTHON=$(python3 - "$OPI_APP_ROOT" <<'PY'
from pathlib import Path
import sys
paths=sorted((Path(sys.argv[1])/'.build-cache/python').glob('cpython-3.12.*-linux-aarch64-gnu/bin/python3.12'))
if not paths: raise SystemExit('未找到 ARM64 Python 3.12')
print(paths[-1])
PY
)
uv pip install --python "$OPI_PYTHON" --break-system-packages pyyaml
mkdir -p "$OPI_APP_ROOT/.build-cache/orangepi-deps"
OPI_XR_DEB="$OPI_APP_ROOT/.build-cache/orangepi-deps/xr-service-arm64.deb"
OPI_XR_SHA=2a6bc1088c77c363fb13030ed0fc8d0f048ec12bbd8ec61a68d2ebd48ed9f942
if ! echo "$OPI_XR_SHA  $OPI_XR_DEB" | sha256sum -c --status; then
  curl -fL --retry 3 --connect-timeout 20 -o "$OPI_XR_DEB.part" \
    'https://github.com/XR-Robotics/XRoboToolkit-PC-Service/releases/download/v1.0.0/XRoboToolkit-PC-Service_1.0.0.0_arm64.deb'
  echo "$OPI_XR_SHA  $OPI_XR_DEB.part" | sha256sum -c -
  mv "$OPI_XR_DEB.part" "$OPI_XR_DEB"
fi
sudo --preserve-env=http_proxy,https_proxy,no_proxy apt-get "${OPI_APT_OPTIONS[@]}" install -y "$OPI_XR_DEB"
"$OPI_PYTHON" "$OPI_APP_ROOT/scripts/build_orangepi_xr.py" --python "$OPI_PYTHON"
"$OPI_PYTHON" "$OPI_APP_ROOT/scripts/build_tianji_native.py"
"$OPI_PYTHON" "$OPI_APP_ROOT/scripts/build_umi_runtime.py" --from-source --jobs 4
OPI_VERSION="0.1.0+$(date -u +%Y%m%d%H%M%S).orangepi"
"$OPI_PYTHON" "$OPI_APP_ROOT/scripts/build_deb.py" --version "$OPI_VERSION" --pico-apk "$OPI_APK"
OPI_DEB="$OPI_APP_ROOT/dist-packages/xiaokai-lab-data-collection_${OPI_VERSION}_arm64.deb"
sudo --preserve-env=http_proxy,https_proxy,no_proxy apt-get "${OPI_APT_OPTIONS[@]}" install -y "$OPI_DEB"
echo "ARM64 安装包：$OPI_DEB"
echo '安装完成。请在开发板桌面应用菜单打开“小凯实验室数采平台”。'
echo '首次使用需在软件中登录、选择本机相机及标定夹爪；真实采集性能须连接硬件验证。'
