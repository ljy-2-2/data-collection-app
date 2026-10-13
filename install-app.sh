#!/usr/bin/env bash
set -euo pipefail
APP_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ $EUID -eq 0 ]]; then
  echo "请用实际使用 App 的普通用户运行安装脚本，不要使用 sudo。" >&2
  exit 1
fi
UMI_ENV="$APP_ROOT/umi_collection/vendor/galaxis/.env"
if [[ ! -f "$UMI_ENV" ]] && [[ -f "$UMI_ENV.example" ]]; then
  install -m 600 "$UMI_ENV.example" "$UMI_ENV"
fi

command -v python3 >/dev/null || { echo "缺少 Python 3.12+" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
  || { echo "需要 Python 3.12+" >&2; exit 1; }

if ! /usr/bin/python3 -c 'import gi; gi.require_version("Secret", "1"); from gi.repository import Secret' 2>/dev/null; then
  echo "提示：记住账号密码需要 gir1.2-secret-1 和已解锁的系统钥匙环（如 gnome-keyring）。未安装时仍可手动登录。"
fi

python3 "$APP_ROOT/scripts/install_desktop.py" --init-env-only
python3 -m venv "$APP_ROOT/.venv"
"$APP_ROOT/.venv/bin/python" -m pip install --upgrade pip
"$APP_ROOT/.venv/bin/python" -m pip install -e "$APP_ROOT/teleop_control"

"$APP_ROOT/.venv/bin/python" -m pip install 'PySide6==6.8.3' 'h5py>=3.11' 'psutil>=6'

python3 "$APP_ROOT/scripts/install_desktop.py"
chmod +x "$APP_ROOT/数据采集台.desktop" "$APP_ROOT/scripts/desktop_app.py"

echo
echo "安装完成。请双击 数据采集台.desktop，或从应用菜单打开“小凯实验室数采平台”。"
echo "命令行维护入口：$APP_ROOT/start-app.sh"
