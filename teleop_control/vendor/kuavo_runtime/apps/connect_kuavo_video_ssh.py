"""Manual video-only SSH connection: kuavons -> AGX, never a robot control call."""
import argparse
from contextlib import contextmanager
import fcntl
import ipaddress
import os
from pathlib import Path
import re
import stat
import subprocess


@contextmanager
def connection_lock(socket):
    # Never inherit this lock into the background SSH Master.
    fd = os.open(socket + '.connect.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
            raise RuntimeError('connection lock is not owned by this user')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def build_connect_command(args):
    network = Path(__file__).resolve().parents[1] / 'scripts' / 'kuavo-network'
    return ['sudo', '-n', str(network), 'run', '--',
            'ssh', '-F', '/dev/null', '-M', '-S', args.ssh_socket,
            '-o', 'ControlPersist=600', '-o', 'ConnectTimeout=5',
            '-o', 'ProxyCommand=none', '-o', 'Compression=no', '-o', 'IPQoS=throughput',
            '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=3',
            '-fN', args.ssh_target]


def check_master(args):
    return subprocess.run(['ssh', '-F', '/dev/null', '-S', args.ssh_socket,
                           '-O', 'check', args.ssh_target], capture_output=True, text=True, timeout=5)


def verify_direct_connection(args):
    result = subprocess.run(['ssh', '-F', '/dev/null', '-T', '-S', args.ssh_socket,
                             '-o', 'BatchMode=yes', '-o', 'ProxyCommand=false',
                             args.ssh_target, 'printenv SSH_CONNECTION'],
                            capture_output=True, text=True, timeout=5)
    fields = result.stdout.split()
    server_ip = args.ssh_target.split('@', 1)[1]
    if (result.returncode != 0 or len(fields) != 4
            or fields[0] != args.client_ip or fields[2] != server_ip):
        raise RuntimeError('not a direct PC-to-AGX connection; existing session left untouched')


def main():
    parser = argparse.ArgumentParser(description='Connect a separate direct AGX video SSH Master')
    parser.add_argument('--ssh-target', default='leju_kuavo@192.168.26.12')
    parser.add_argument('--ssh-socket', default='/tmp/kuavo5-agx-video-ssh-master')
    parser.add_argument('--client-ip', default='192.168.26.250')
    parser.add_argument('--require-existing', action='store_true',
                        help='verify existing video login only; never authenticate or run sudo')
    args = parser.parse_args()
    try:
        user, host = args.ssh_target.split('@', 1)
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', user):
            raise ValueError('invalid SSH user')
        ipaddress.IPv4Address(host)
        ipaddress.IPv4Address(args.client_ip)
        if not Path(args.ssh_socket).is_absolute():
            raise ValueError('SSH socket must be an absolute path')
        args.ssh_socket = str(Path(args.ssh_socket).resolve())
        if args.ssh_socket in ('/tmp/kuavo5-ssh-master', '/tmp/kuavo5-agx-ssh-master'):
            raise ValueError('use a separate video socket, not a control or proxy Master')
        with connection_lock(args.ssh_socket):
            reused = check_master(args).returncode == 0
            if not reused:
                if args.require_existing:
                    raise RuntimeError('视频登录尚未就绪；在电脑终端运行 bash scripts/kuavo-arms-network --prepare-video；此步骤只准备视频连接')
                if os.path.lexists(args.ssh_socket):
                    raise RuntimeError('socket path already exists; left untouched; choose another --ssh-socket')
                print('KUAVO_VIDEO_SSH_CONNECT direct_agx=True robot_commands_sent=False', flush=True)
                # Only this explicit manual entry may ask for the AGX password.
                # No secret is stored in argv, environment, files or project config.
                result = subprocess.run(build_connect_command(args), close_fds=True)
                if result.returncode != 0:
                    raise RuntimeError('video SSH connection failed; check sudo -v, kuavons and AGX authentication')
                if check_master(args).returncode != 0:
                    raise RuntimeError('new video SSH Master is unavailable')
            verify_direct_connection(args)
            print(f'KUAVO_VIDEO_SSH_READY socket={args.ssh_socket} reused={reused} '
                  'direct_agx=True robot_commands_sent=False', flush=True)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f'KUAVO_VIDEO_SSH_ERROR reason={error} robot_commands_sent=False', flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
