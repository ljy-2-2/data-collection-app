"""Reject a conflicting local monitor listener before taking over devices."""
import errno
import socket
from urllib.parse import urlsplit


def require_free_monitor_port(url):
    address = urlsplit(url)
    if address.hostname not in ('127.0.0.1', 'localhost', '::1'):
        return
    port = address.port or (443 if address.scheme == 'https' else 80)
    host = '::1' if address.hostname == '::1' else '127.0.0.1'
    family = socket.AF_INET6 if host == '::1' else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, port))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise RuntimeError(
                f'采集监控端口 {port} 已被其他程序占用，无法显示相机和传感器数据。'
                '请关闭占用该端口的程序后重新进入监控。') from exc
        raise RuntimeError(f'无法使用采集监控端口 {port}：{exc}') from exc
