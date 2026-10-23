"""Borrow one unconnected TCP socket from the existing kuavons namespace.

Only the short helper enters kuavons. The PICO SDK and TLS remain on the host.
The helper creates/binds a socket and passes its fd over an owner-only Unix
socket; it does not connect to the robot, publish data or configure networking.
"""
import array
import errno
import ipaddress
import os
from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time

BIND_ADDRESS = '192.168.26.250'
NAMESPACE = 'kuavons'


def _ipv4(value):
    return str(ipaddress.IPv4Address(value))


def _finish_helper(process):
    """Reap only our short socket helper; never signal a controller/session."""
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=.5)
    if process.stderr is not None:
        process.stderr.close()


def acquire_socket(command_factory, *, bind_address, timeout_s=5.):
    """Receive and verify one bound, unconnected IPv4 stream fd from a helper.

    command_factory is trusted local configuration, never remotely supplied.
    Inputs have a whole-operation timeout. No network connection is attempted.
    """
    bind_address = _ipv4(bind_address)
    if type(timeout_s) not in (int, float) or not 0 < timeout_s <= 10:
        raise ValueError('helper timeout must be finite and within (0, 10] seconds')
    process, result, descriptors = None, None, []
    with tempfile.TemporaryDirectory(prefix='kuavo-session-tcp-') as directory:
        path = directory+'/socket'
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.monotonic()+timeout_s
        try:
            listener.bind(path)
            listener.listen(1)
            listener.setblocking(False)
            process = subprocess.Popen(command_factory(path), stdout=subprocess.DEVNULL,
                                       stderr=subprocess.PIPE)
            os.set_blocking(process.stderr.fileno(), False)
            while True:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('TCP namespace helper timed out; check sudo -v and existing kuavons')
                if select.select([listener], [], [], min(.02, remaining))[0]:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('TCP namespace helper deadline expired')
                    break
                if process.poll() is not None:
                    try:
                        detail = os.read(process.stderr.fileno(), 4096).decode(errors='replace')[-1000:].strip()
                    except BlockingIOError:
                        detail = ''
                    raise RuntimeError('TCP namespace helper failed; run sudo -v and check kuavons: '+detail)
            connection, _ = listener.accept()
            with connection:
                _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != os.getuid():
                    raise ValueError('TCP socket helper UID mismatch')
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('TCP namespace helper deadline expired')
                connection.settimeout(remaining)
                message, ancillary, flags, _ = connection.recvmsg(
                    1, socket.CMSG_SPACE(array.array('i').itemsize*8), socket.MSG_CMSG_CLOEXEC)
                for level, kind, data in ancillary:
                    if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                        values = array.array('i')
                        values.frombytes(data[:len(data)//values.itemsize*values.itemsize])
                        descriptors.extend(values)
                if message != b'T' or flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC) or len(descriptors) != 1:
                    raise ValueError('TCP socket helper descriptor mismatch')
                if time.monotonic() >= deadline:
                    raise TimeoutError('TCP namespace helper deadline expired')
                descriptor = descriptors[0]
                result = socket.socket(fileno=descriptor)
                descriptors.pop()
                if (result.family != socket.AF_INET
                    or result.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
                    or result.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
                    or result.getsockname()[0] != bind_address):
                    raise ValueError('TCP socket helper returned wrong socket type or bind address')
                try:
                    result.getpeername()
                except OSError as exc:
                    if exc.errno != errno.ENOTCONN:
                        raise
                else:
                    raise ValueError('TCP socket helper must not return a connected socket')
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('TCP namespace helper deadline expired')
            _, stderr = process.communicate(timeout=remaining)
            if process.returncode:
                raise RuntimeError('TCP namespace helper failed: '+stderr.decode(errors='replace')[-1000:])
            return result
        except BaseException:
            if result is not None:
                result.close()
            raise
        finally:
            listener.close()
            for fd in descriptors:
                os.close(fd)
            _finish_helper(process)


def namespace_socket(runtime_root):
    """Explicitly use the existing managed kuavons; never create routes/fallback."""
    root = Path(runtime_root).resolve()
    helper = root/'src/teleop/robots/kuavo5_session_tcp_socket.py'
    # env fixes namespace and source address independently of caller overrides.
    # kuavo-network run verifies its ownership alias and drops to invoking user.
    return acquire_socket(lambda path: [
        'sudo', '-n', 'env', 'KUAVO_NETNS='+NAMESPACE,
        str(root/'scripts/kuavo-network'), 'run', '--',
        '/usr/bin/python3', '-B', str(helper), path, BIND_ADDRESS],
        bind_address=BIND_ADDRESS)


def pass_socket(path, bind_address):
    """Helper entry: bind only. TCP connect and pinned TLS happen on the host."""
    bind_address = _ipv4(bind_address)
    if not Path(path).is_absolute():
        raise ValueError('absolute local socket path required')
    # Do not become a daemon if the parent disappears during handoff.
    signal.alarm(8)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
            tcp.bind((bind_address, 0))
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(2.)
                connection.connect(path)
                connection.sendmsg([b'T'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                            array.array('i', [tcp.fileno()]))])
    finally:
        signal.alarm(0)


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('usage: socket-helper UNIX_PATH BIND_IP')
    pass_socket(sys.argv[1], sys.argv[2])
