#!/usr/bin/env python3
"""Create per-host offline HTTPS service installers; never deploy or connect."""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tarfile

ROOT=Path(__file__).resolve().parents[1]
HOSTS=[('192.168.26.1','lab','/home/lab/kuavo-ros-control/devel/setup.bash',True),
       ('192.168.26.22','jt','/opt/ros/noetic/setup.bash',False),
       ('192.168.26.12','leju_kuavo','/home/leju_kuavo/kuavo_ros_application/devel/setup.bash',False)]

def main():
    os.umask(0o077)
    network=ROOT/'teleop_control/runtime/kuavo/network';output=ROOT/'teleop_control/runtime/kuavo/network-install'
    network.mkdir(parents=True,exist_ok=True);output.mkdir(parents=True,exist_ok=True)
    for host,user,setup,joints in HOSTS:
        local=network/host;local.mkdir(exist_ok=True)
        folder=output/host;folder.mkdir(exist_ok=True)
        cert,key=folder/'server.crt',folder/'server.key'
        if not cert.exists() or not key.exists():
            subprocess.run(['openssl','req','-x509','-newkey','rsa:3072','-nodes','-days','3650',
                '-keyout',str(key),'-out',str(cert),'-subj','/CN='+host,'-addext','subjectAltName=IP:'+host],check=True,capture_output=True)
        token=local/'token'
        if not token.exists():token.write_text(secrets.token_hex(32))
        shutil.copy2(token,folder/'token');shutil.copy2(cert,local/'server.crt')
        (local/'client.json').write_text(json.dumps(dict(url='https://'+host+':7444',ca_file='server.crt',token_file='token'),indent=2))
        install='/home/'+user+'/.local/share/kuavo-app-network'
        (folder/'config.json').write_text(json.dumps(dict(bind=host,port=7444,joint_feedback=joints,
            token_file=install+'/token',cert_file=install+'/server.crt',key_file=install+'/server.key'),indent=2))
        for name in ('kuavo_network_agent.py','kuavo_camera_remote.py','kuavo_joint_remote.py'):
            shutil.copy2(ROOT/'teleop_control/src/teleop_control'/name,folder/name)
        (folder/'run.sh').write_text('#!/bin/bash\nset -e\nsource '+setup+'\nexport ROS_MASTER_URI=http://192.168.26.1:11311\nexport ROS_IP='+host+'\nunset ROS_HOSTNAME\ncd '+install+'\nexec /usr/bin/python3 -u kuavo_network_agent.py --config config.json\n')
        (folder/'kuavo-app-network.service').write_text('[Unit]\nDescription=App camera and read-only joint network service\nAfter=network-online.target\nWants=network-online.target\n[Service]\nType=simple\nUser='+user+'\nSupplementaryGroups=video\nWorkingDirectory='+install+'\nExecStart=/bin/bash '+install+'/run.sh\nRestart=on-failure\nRestartSec=2\nNoNewPrivileges=true\nPrivateTmp=true\n[Install]\nWantedBy=multi-user.target\n')
        (folder/'install.sh').write_text(('''#!/bin/bash
set -eu
cd "$(dirname "$0")"
if [ "$(id -u)" != 0 ]; then echo '请在这台机器本机终端执行 sudo bash install.sh'; exit 1; fi
TARGET='''+install+'''
ACCOUNT='''+user+'''
id "$ACCOUNT" >/dev/null
hostname -I | tr ' ' '\\n' | /usr/bin/grep -Fx @HOST@ >/dev/null || { echo '安装包与本机地址不符'; exit 1; }
test -f @SETUP@
/usr/bin/python3 -c 'import ssl'''+('' if joints else '; import cv2')+''''
if systemctl is-active --quiet kuavo-app-network.service; then echo '服务已运行，请先核对版本，不自动替换活动数据流'; exit 1; fi
install -d -m 700 -o "$ACCOUNT" -g "$(id -gn "$ACCOUNT")" "$TARGET"
for name in kuavo_network_agent.py kuavo_camera_remote.py kuavo_joint_remote.py config.json server.crt server.key token run.sh; do
 install -m 600 -o "$ACCOUNT" -g "$(id -gn "$ACCOUNT")" "$name" "$TARGET/$name"
done
install -m 644 kuavo-app-network.service /etc/systemd/system/kuavo-app-network.service
systemctl daemon-reload
systemctl enable --now kuavo-app-network.service
systemctl is-active kuavo-app-network.service
''').replace('@HOST@',host).replace('@SETUP@',setup))
        (folder/'README.txt').write_text('安装到 '+host+'（账号 '+user+'）。通过 U 盘或文件管理器复制本目录到该工控机，在该机终端执行 sudo bash install.sh。\n服务启动仅监听 HTTPS，不打开相机、不启动机器人主控、不发布任何机器人命令。App 检查设备后才请求数据流。\n不要运行错误主机的安装包。保留 token/server.key 私密。\n此包不解除机器人上一会话的清理锁。\n')
        archive=output/('kuavo-network-'+host+'.tar.gz')
        with tarfile.open(archive,'w:gz') as tar:tar.add(folder,arcname=host)
        print(archive)

if __name__=='__main__':main()
