#!/usr/bin/env python3
"""One-time update of our own idle camera service; never opens USB or robot SDK."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

SDK=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SDK/'FusionCameraSDK'))
from fusion_camera.stream import FusionStreamClient

EXPECTED={
    'server.py':'46663fb5e4410cdc05661ac21c8cbf8f29b2feb83c04af4718d97b1ab0f37d80',
    'streaming.py':'445a006e8e4efe618447422673e37c233c3ca3eded7107517131308a1b051293'}

REMOTE=r'''
from __future__ import print_function
import base64,hashlib,json,os,subprocess,time
payload=json.loads(base64.b64decode(PAYLOAD))
root='/mnt/fusion-camera'
def digest(data):return hashlib.sha256(data).hexdigest()
def idle():
    code=subprocess.call(['/usr/bin/pgrep','-x','capture-three'],stdout=open(os.devnull,'wb'))
    if code != 1:raise RuntimeError('Camera capture exists or pgrep failed; refusing update')
conf=open('/etc/init/fusion-camera.conf').read()
if '/mnt/fusion-camera/server.py' not in conf:
    raise RuntimeError('Unexpected service layout; inspect manually')
idle()
new={name:base64.b64decode(v) for name,v in payload['files'].items()}
old={name:open(os.path.join(root,name),'rb').read() for name in new}
if all(digest(old[k])==digest(new[k]) for k in new):
    print('Already updated; no service restart')
    raise SystemExit(0)
for name,data in new.items():
    if digest(old[name]) != payload['expected'][name]:
        raise RuntimeError('Existing '+name+' differs from reviewed v2.1; refusing overwrite')
    compile(data,name,'exec')
release=os.path.join(root,'releases','collection-v22-'+time.strftime('%Y%m%d-%H%M%S'))
os.mkdir(release)
for name,data in old.items():
    with open(os.path.join(release,name),'wb') as f:f.write(data)
for name,data in new.items():
    with open(os.path.join(release,'new-'+name),'wb') as f:f.write(data)
    os.chmod(os.path.join(release,'new-'+name),os.stat(os.path.join(root,name)).st_mode & 511)
idle()
subprocess.check_call(['/sbin/stop','fusion-camera'])
try:
    idle()
    for name in new:os.rename(os.path.join(release,'new-'+name),os.path.join(root,name))
    subprocess.check_call(['/sbin/start','fusion-camera'])
    time.sleep(1)
    status=subprocess.check_output(['/sbin/status','fusion-camera'])
    if b'start/running' not in status:raise RuntimeError('Updated service not running')
except Exception:
    subprocess.call(['/sbin/stop','fusion-camera'])
    for name,data in old.items():
        path=os.path.join(root,name+'.rollback')
        with open(path,'wb') as f:f.write(data)
        os.chmod(path,os.stat(os.path.join(release,name)).st_mode & 511)
        os.rename(path,os.path.join(root,name))
    subprocess.check_call(['/sbin/start','fusion-camera'])
    raise
print('Updated our idle camera service. Backup: '+release)
'''

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--key',default=str(Path.home()/'.ssh/id_ed25519_192_168_1_190'))
    a=p.parse_args()
    client=FusionStreamClient(credentials=SDK/'FusionCameraSDK/client.json',timeout=5)
    status=client.status()
    if status.get('fault') or not status.get('idle'):
        raise RuntimeError('相机服务忙或处于故障；先人工检查，更新器不清除故障')
    files={name:base64.b64encode((Path(__file__).parent/'controller_v22'/name).read_bytes()).decode()
           for name in EXPECTED}
    payload=base64.b64encode(json.dumps({'files':files,'expected':EXPECTED}).encode()).decode()
    script='PAYLOAD='+repr(payload)+'\n'+REMOTE
    # Future import must remain the first executable statement in Python 2.
    script=script.replace('from __future__ import print_function\n','')
    script='from __future__ import print_function\n'+script
    command=['ssh','-i',a.key,'-o','IdentitiesOnly=yes','-o','BatchMode=yes',
             '-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8',
             'root@192.168.1.190','/usr/bin/python2.7 -']
    subprocess.run(command,input=script,text=True,check=True,timeout=35)
    status=client.status()
    if 'teleop_observation' not in status.get('observation_modes',[]) or not status.get('idle'):
        raise RuntimeError('服务更新后的接口验收失败；停止使用并检查上方持久化备份，不要启动采集')
    print('相机服务 v2.2 已就绪；没有启动相机采集，没有连接机器人 SDK。')
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,RuntimeError,subprocess.SubprocessError) as e:
        print('未通过更新检查：'+str(e),file=sys.stderr)
        raise SystemExit(1)
