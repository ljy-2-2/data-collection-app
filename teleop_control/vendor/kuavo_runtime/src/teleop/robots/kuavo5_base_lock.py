"""One chassis flock inode shared by privileged unified and legacy lab entries.

Import is inert. No lock is deleted or replaced, and existing contents and modes
are retained. Returning an open file transfers lock lifetime to the caller.
"""
import fcntl
import os
from pathlib import Path
import pwd
import stat


BASE_CONTROL_LOCK_PATH = '/tmp/teleop-kuavo5-base.lock'


def acquire_base_lock(path=BASE_CONTROL_LOCK_PATH):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('absolute chassis lock path required')
    uid, euid = os.getuid(), os.geteuid()
    if uid != euid:
        raise PermissionError('mixed real/effective user identity is not supported')
    owner_uid, owner_gid = uid, os.getgid()
    if euid == 0:
        # The installed damiao worker is root; the independent base entry is lab.
        # Never trust caller/environment-selected owners for a shared robot lock.
        try:
            operator = pwd.getpwnam('lab')
            owner_uid, owner_gid = operator.pw_uid, operator.pw_gid
        except KeyError as exc:
            raise PermissionError('fixed lab operator account is unavailable') from exc
        if type(owner_uid) is not int or owner_uid <= 0:
            raise PermissionError('fixed lab operator must be non-root')
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    fd = None
    created = False
    for _ in range(3):
        try:
            # No O_CREAT on existing lab-owned files: Linux protected_regular
            # otherwise refuses root in sticky /tmp even when permission is valid.
            fd = os.open(str(path), flags)
            break
        except FileNotFoundError:
            try:
                fd = os.open(str(path), flags | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
                break
            except FileExistsError:
                continue
    if fd is None:
        raise RuntimeError('chassis lock changed repeatedly during acquisition')
    try:
        if created:
            os.fchmod(fd, 0o600)
            if owner_uid != euid:
                os.fchown(fd, owner_uid, -1)
        info = os.fstat(fd)
        # Legacy open(..., 'a') leaves 0644 or 0664 depending on the umask.
        # Permit its operator primary group, never an unrelated writable group.
        # File contents are not an authority token and are never changed here.
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != owner_uid or info.st_mode & 0o002
                or (info.st_mode & 0o020 and info.st_gid != owner_gid)
                or info.st_mode & 0o600 != 0o600):
            raise ValueError('chassis lock must be a single regular file owned and writable '
                             'by the expected operator, without unrelated write access')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('another base robot session owns the control lock') from exc
        result = os.fdopen(fd, 'r+b', buffering=0)
        fd = None
        return result
    finally:
        if fd is not None:
            os.close(fd)
