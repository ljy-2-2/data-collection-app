#!/usr/bin/env bash
# Distributed beside bundle.env, SHA256SUMS and a flat, local APT repository.
set -euo pipefail
BUNDLE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
case "${1:-}" in
  ''|--check) ;;
  *) echo "用法：bash install.sh [--check]" >&2; exit 2 ;;
esac
MODE=${1:-install}
cd "$BUNDLE"
sha256sum --check --quiet SHA256SUMS || { echo "安装文件损坏或不完整，请重新复制整个离线包。" >&2; exit 1; }
source ./bundle.env
source /etc/os-release
if [[ ${ID:-} != ubuntu || ${VERSION_ID:-} != "$TARGET_UBUNTU" || $(dpkg --print-architecture) != "$TARGET_ARCH" ]]; then
  echo "此包仅适用于 Ubuntu $TARGET_UBUNTU / $TARGET_ARCH；当前系统不匹配，未安装。" >&2
  exit 1
fi
if [[ $EUID -ne 0 ]]; then
  exec sudo -- bash "$BUNDLE/install.sh" "${@:1}"
fi
STATE=$(mktemp -d /var/tmp/xiaokai-offline.XXXXXXXX)
trap 'rm -rf -- "$STATE"' EXIT
chmod 755 "$STATE"
mkdir -p "$STATE/lists/partial" "$STATE/archives/partial" "$STATE/empty"
touch "$STATE/empty.conf"
printf 'Dir::Etc::parts "%s/empty";\nDir::Etc::main "%s/empty.conf";\n' "$STATE" "$STATE" > "$STATE/apt.conf"
export APT_CONFIG="$STATE/apt.conf"
ln -s "$BUNDLE/repo" "$STATE/repo"
printf 'deb [trusted=yes] file:%s/repo ./\n' "$STATE" > "$STATE/sources.list"
OPTIONS=(
  -o "Dir::Etc::sourcelist=$STATE/sources.list"
  -o "Dir::Etc::sourceparts=$STATE/empty"
  -o "Dir::Etc::preferences=$STATE/empty.conf"
  -o "Dir::Etc::preferencesparts=$STATE/empty"
  -o "Dir::State::lists=$STATE/lists"
  -o "Dir::Cache::archives=$STATE/archives"
  -o APT::Sandbox::User=root
  -o Acquire::Languages=none
  -o APT::Update::Error-Mode=any
)
echo "正在读取随包提供的本地依赖，无需访问网络软件源…"
apt-get "${OPTIONS[@]}" update
# Solve against the recipient's installed packages; refuse removals or downgrades.
apt-get "${OPTIONS[@]}" --simulate --no-remove install "$APP_PACKAGE=$APP_VERSION"
if [[ $MODE == --check ]]; then
  echo "离线依赖检查通过；没有安装软件。"
  exit 0
fi
apt-get "${OPTIONS[@]}" --yes --no-remove install "$APP_PACKAGE=$APP_VERSION"
echo "安装完成。请从应用菜单打开小凯实验室数采平台，使用云端账号登录。"
