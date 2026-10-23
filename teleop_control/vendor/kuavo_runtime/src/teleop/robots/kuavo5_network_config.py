"""Local validation shared by direct clients/listeners. No implicit network I/O."""
import ipaddress
import os
import re
import stat


def validate_numeric_host(host):
    """Numeric IPv4 only: do not resolve names or discover robot addresses."""
    if not isinstance(host, str):
        raise ValueError('numeric IPv4 address required')
    try:
        return str(ipaddress.IPv4Address(host))
    except ipaddress.AddressValueError as exc:
        raise ValueError('numeric IPv4 address required') from exc


def validate_endpoint(host, port, cert_sha256=None):
    host = validate_numeric_host(host)
    if type(port) is not int or not 0 < port <= 65535:
        raise ValueError('network port must be within 1..65535')
    if cert_sha256 is not None:
        if not isinstance(cert_sha256, str) or not re.fullmatch('[0-9a-fA-F]{64}', cert_sha256):
            raise ValueError('TLS certificate pin must be 64 hexadecimal SHA256 characters')
        cert_sha256 = cert_sha256.lower()
    if not ipaddress.IPv4Address(host).is_loopback and cert_sha256 is None:
        raise ValueError('non-loopback network sessions require --network-cert-sha256 (TLS pin)')
    return host, port, cert_sha256


def read_auth_key(path):
    """Read exactly one owner-only 32-byte token; never print its contents."""
    fd = os.open(os.fspath(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('token file must be a regular file owned by this user with mode 0600 or 0400')
        data = os.read(fd, 128)
        if os.read(fd, 1):
            raise ValueError('token file too large')
        try:
            token = data.decode('ascii').strip()
        except UnicodeDecodeError as exc:
            raise ValueError('token file must contain 64 hexadecimal characters') from exc
        if not re.fullmatch('[0-9a-fA-F]{64}', token):
            raise ValueError('token file must contain 64 hexadecimal characters')
        return bytes.fromhex(token)
    finally:
        os.close(fd)
