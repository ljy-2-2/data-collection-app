#!/usr/bin/env python3
"""Restore the existing laptop public key using one interactive root login.

Run in the user's local terminal. Password is read by OpenSSH from /dev/tty;
this script never receives, stores or prints it. No robot SDK/service commands.
"""
import base64
import json
from pathlib import Path
import subprocess
import sys

REMOTE = r'''
from __future__ import print_function
import base64,json,os,stat,tempfile,time
if os.geteuid()!=0:raise RuntimeError('Root login required')
key=base64.b64decode(KEY).strip()
parts=key.split()
if len(parts)<2 or parts[0]!=b'ssh-ed25519':raise RuntimeError('Unexpected public key format')
for p in ('/root','/mnt/fusion-camera'):
    s=os.lstat(p)
    if not stat.S_ISDIR(s.st_mode) or s.st_uid!=0:raise RuntimeError('Unexpected owner or path: '+p)
sshdir='/root/.ssh'
auth=sshdir+'/authorized_keys'
for p in (sshdir,auth):
    if os.path.lexists(p):
        s=os.lstat(p)
        if stat.S_ISLNK(s.st_mode) or s.st_uid!=0:raise RuntimeError('Refusing unexpected path/owner: '+p)
        if p==sshdir and not stat.S_ISDIR(s.st_mode):raise RuntimeError('Not a directory: '+p)
        if p==auth and not stat.S_ISREG(s.st_mode):raise RuntimeError('Not a regular file: '+p)
base='/mnt/fusion-camera/ssh-recovery'
if not os.path.exists(base):os.mkdir(base,0o700)
s=os.lstat(base)
if not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or s.st_mode & 0o022:
    raise RuntimeError('Unsafe recovery directory')
backup=tempfile.mkdtemp(prefix=time.strftime('%Y%m%d-%H%M%S-'),dir=base)
old=open(auth,'rb').read() if os.path.exists(auth) else b''
modes={p:oct(os.lstat(p).st_mode & 0o7777) if os.path.lexists(p) else None
       for p in ('/root',sshdir,auth)}
with open(backup+'/previous_modes.json','w') as f:json.dump(modes,f)
with open(backup+'/authorized_keys.before','wb') as f:f.write(old)
os.chmod(backup+'/authorized_keys.before',0o600)
with open(backup+'/laptop_public_key.pub','wb') as f:f.write(key+b'\n')
os.chmod('/root',0o700)
if not os.path.exists(sshdir):os.mkdir(sshdir,0o700)
os.chmod(sshdir,0o700)
# Preserve existing authorized keys, including lines with SSH options.
exists=any(parts[1] in line.split() for line in old.splitlines() if not line.lstrip().startswith(b'#'))
content=old if exists else old+(b'\n' if old and not old.endswith(b'\n') else b'')+key+b'\n'
fd,path=tempfile.mkstemp(prefix='.authorized_keys-',dir=sshdir)
try:
    with os.fdopen(fd,'wb') as f:
        f.write(content);f.flush();os.fsync(f.fileno())
    os.chmod(path,0o600)
    os.rename(path,auth)
finally:
    if os.path.exists(path):os.unlink(path)
print('SSH public key restored; previous modes='+json.dumps(modes))
print('Backup: '+backup)
print('No SSH daemon restart, camera startup or robot command was performed.')
'''


def main():
    if not sys.stdin.isatty():
        raise RuntimeError('请在你自己的本机终端运行，以便 OpenSSH 安全提示输入密码')
    keypath=Path.home()/'.ssh/id_ed25519_192_168_1_190'
    public=Path(str(keypath)+'.pub').read_bytes().strip()
    if not public.startswith(b'ssh-ed25519 '):raise ValueError('Unexpected public key')
    print('将恢复本机已有公钥及 /root 的 SSH 权限。只在 OpenSSH 密码提示中输入控制器 root 密码。',flush=True)
    script=REMOTE.replace('import base64,json,os,stat,tempfile,time',
        'import base64,json,os,stat,tempfile,time\nKEY='+repr(base64.b64encode(public).decode()))
    common=['ssh','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8']
    subprocess.run(common+['-o','PubkeyAuthentication=no','-o','PreferredAuthentications=password',
        '-o','NumberOfPasswordPrompts=1','root@192.168.1.190','/usr/bin/python2.7 -'],
        input=script,text=True,check=True)
    subprocess.run(common+['-i',str(keypath),'-o','IdentitiesOnly=yes','-o','BatchMode=yes',
        'root@192.168.1.190','printf SSH_KEY_AUTH_OK'],check=True)
    print('\n密钥登录验证通过。请告知助手继续检查重启持久化并完成独立采集服务更新。')
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,RuntimeError,ValueError,subprocess.SubprocessError) as exc:
        print('修复未完成：'+str(exc),file=sys.stderr)
        raise SystemExit(1)
