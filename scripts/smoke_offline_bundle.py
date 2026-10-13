#!/usr/bin/env python3
"""Exercise offline resolution and actual dependency installation in clean Ubuntu.

No host Docker socket is exposed. App postinst needs a real systemd/Docker host,
so the test unpacks the app and checks its runtime, without pretending to test
that final host integration or attached collection hardware.
"""
import argparse
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--ubuntu", choices=("22.04", "24.04"), required=True)
    args = parser.parse_args()
    bundle = args.bundle.resolve(strict=True)
    script = r'''
set -euo pipefail
bash /bundle/install.sh --check
test ! -S /var/run/docker.sock
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod +x /usr/sbin/policy-rc.d
printf 'Dir::Etc::parts "-";\nDir::Etc::main "-";\n' > /tmp/offline-apt.conf
export APT_CONFIG=/tmp/offline-apt.conf
printf 'deb [trusted=yes] file:/bundle/repo ./\n' > /tmp/offline.sources.list
opts=(-o Dir::Etc::sourcelist=/tmp/offline.sources.list -o Dir::Etc::sourceparts=- -o APT::Sandbox::User=root)
apt-get "${opts[@]}" update
app=(/bundle/repo/xiaokai-lab-data-collection_*.deb)
test "${#app[@]}" = 1
dependencies=$(dpkg-deb -f "${app[0]}" Depends)
DEBIAN_FRONTEND=noninteractive apt-get "${opts[@]}" --yes --no-remove satisfy "$dependencies"
echo 'PASS: actual system dependencies installed with Docker networking disabled'
dpkg --unpack "${app[0]}"
/opt/xiaokai-lab-data-collection/runtime/python/bin/python3.12 -c 'import fastapi, httpx, cv2, serial, yaml, psutil, PySide6.QtQuick, PySide6.QtQuick3D; print("PASS: application Python and Qt imports in clean offline Ubuntu")'
docker compose version
adb version
echo 'PASS: Docker Compose and ADB executable dependencies'
'''
    subprocess.run([
        "docker", "run", "--rm", "--network", "none", "--interactive",
        "--mount", f"type=bind,source={bundle},target=/bundle,readonly",
        f"ubuntu:{args.ubuntu}", "bash", "-s",
    ], input=script, text=True, check=True)


if __name__ == "__main__":
    main()
