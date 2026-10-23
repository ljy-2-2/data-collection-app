"""Transfer one UDP socket out of kuavons; never configure routes or send data.

The helper runs via the existing kuavo-network run privilege-drop wrapper.
Its socket retains the network namespace when passed through SCM_RIGHTS;
the PICO SDK stays in the host namespace. No daemon, credentials or ROS here.
"""
import array
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import tempfile
import time


def acquire_socket(command_factory, *, timeout_s=5.):
    process = None
    descriptors = []
    result = None
    with tempfile.TemporaryDirectory(prefix='kuavo-base-udp-') as directory:
        path = directory+'/socket'
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(path)
            listener.listen(1)
            # Keep this short helper in the operator's terminal session so
            # sudo -n can reuse that terminal's sudo -v credential timestamp.
            process = subprocess.Popen(command_factory(path), stdout=subprocess.DEVNULL,
                                       stderr=subprocess.PIPE)
            deadline = time.monotonic()+timeout_s
            while not select.select([listener], [], [], .02)[0]:
                if process.poll() is not None:
                    detail=process.stderr.read().decode(errors='replace')[-1000:].strip()
                    raise RuntimeError('UDP socket helper failed; run sudo -v in this terminal: '+detail)
                if time.monotonic() >= deadline:
                    raise RuntimeError('UDP socket helper unavailable; run sudo -v and check kuavons')
            connection, _ = listener.accept()
            with connection:
                _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != os.getuid(): raise RuntimeError('UDP socket helper UID mismatch')
                connection.settimeout(max(.01, deadline-time.monotonic()))
                message, ancillary, flags, _ = connection.recvmsg(1, socket.CMSG_SPACE(4*8), socket.MSG_CMSG_CLOEXEC)
                for level, kind, data in ancillary:
                    if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                        values = array.array('i'); values.frombytes(data[:len(data)//values.itemsize*values.itemsize])
                        descriptors.extend(values)
                if message != b'S' or flags & socket.MSG_CTRUNC or len(descriptors) != 1:
                    raise RuntimeError('UDP socket helper descriptor mismatch')
                result = socket.socket(fileno=descriptors.pop())
                if result.family != socket.AF_INET or result.type != socket.SOCK_DGRAM:
                    raise RuntimeError('UDP socket helper returned wrong socket type')
            _, stderr = process.communicate(timeout=max(.01, deadline-time.monotonic()))
            if process.returncode: raise RuntimeError('UDP socket helper failed: '+stderr.decode(errors='replace')[-500:])
            return result
        except BaseException:
            if result is not None: result.close()
            raise
        finally:
            listener.close()
            for fd in descriptors: os.close(fd)
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try: process.wait(timeout=.5)
                    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=.5)
                if process.stderr is not None: process.stderr.close()


def namespace_socket(runtime_root):
    root = Path(runtime_root)
    helper = root/'src/teleop/robots/kuavo5_base_udp_socket.py'
    return acquire_socket(lambda path: ['sudo', '-n', str(root/'scripts/kuavo-network'),
        'run', '--', '/usr/bin/python3', '-B', str(helper), path, '192.168.26.250'])


def pass_socket(path, bind_address):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.bind((bind_address, 0))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(path)
            connection.sendmsg([b'S'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [udp.fileno()]))])


if __name__ == '__main__':
    if len(sys.argv) != 3: raise SystemExit('usage: socket-helper UNIX_PATH BIND_IP')
    pass_socket(sys.argv[1], sys.argv[2])
