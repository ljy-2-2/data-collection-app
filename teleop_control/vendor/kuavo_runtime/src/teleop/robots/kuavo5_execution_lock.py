"""Local exclusive execution gate shared by current SSH and network arm entries.

Pure stdlib, Python 3.8 compatible. Construction/import is inert. Acquire happens
before run_robot, hence before ROS imports, publishers or mode requests. The
lock remains held through the complete core shutdown; never unlink the file.
"""
import fcntl
import json
import os
import pwd
from pathlib import Path
import re
import stat

EXECUTION_LOCK_PATH = Path('/tmp/kuavo5-arms-execution.lock')
NETWORK_OWNER_PATH = Path('/tmp/kuavo5-arms-network-owner.json')


def _private_regular(fd, label, owner_uid):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o077:
        raise ValueError(label + ' must be an owner-only regular file owned by the expected operator')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate network ownership field')
        result[key] = value
    return result


def _network_owner(path, owner_uid):
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    try:
        _private_regular(fd, 'network owner marker', owner_uid)
        raw = os.read(fd, 8193)
        if not raw or len(raw) > 8192:
            raise ValueError('network owner marker is empty/oversize; cleanup is unconfirmed')
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or type(value.get('active')) is not bool:
            raise ValueError('network owner marker is invalid; cleanup is unconfirmed')
        if value['active']:
            identity_field = 'request_id' if value.get('operation') == 'preparation' else 'session_id'
            if (type(value.get('supervisor_pid')) is not int or value['supervisor_pid'] <= 0
                    or not isinstance(value.get(identity_field), str)
                    or re.fullmatch('[0-9a-f]{32}', value[identity_field]) is None):
                raise ValueError('active network owner identity is invalid')
            if value.get('operation') == 'preparation' and (
                    value.get('action') not in ('start', 'initialize', 'grippers_prepare')
                    or not isinstance(value.get('installation_sha256'), str)
                    or re.fullmatch('[0-9a-f]{64}', value['installation_sha256']) is None):
                raise ValueError('preparation owner identity is invalid')
        return value
    finally:
        os.close(fd)


class ExecutionLock:
    """Hold one shared execution slot; privileged grippers retain lab ownership.

    trusted_operator_uid is accepted only for privileged workers and must match the fixed
    lab account. It never selects a different path or bypasses a dirty ledger.
    Fake tests inject temporary paths and mocked privilege/account metadata.
    """
    def __init__(self, *, route, lock_path=EXECUTION_LOCK_PATH, ledger_path=NETWORK_OWNER_PATH,
                 trusted_operator_uid=None, preparation_request_id=None, preparation_installation_sha256=None):
        if route not in ('ssh', 'network', 'preparation'):
            raise ValueError('explicit ssh/network/preparation execution route required')
        if trusted_operator_uid is not None and (type(trusted_operator_uid) is not int or trusted_operator_uid < 1):
            raise ValueError('explicit non-root trusted operator UID required')
        self.trusted_operator_uid = trusted_operator_uid
        if route == 'preparation':
            if trusted_operator_uid is None:
                raise ValueError('preparation route requires privileged fixed-operator helper')
            for value, size in ((preparation_request_id, 32), (preparation_installation_sha256, 64)):
                if not isinstance(value, str) or re.fullmatch('[0-9a-f]{%d}' % size, value) is None:
                    raise ValueError('exact preparation request and installation identity required')
        elif preparation_request_id is not None or preparation_installation_sha256 is not None:
            raise ValueError('preparation identity only belongs to preparation route')
        self.preparation_request_id = preparation_request_id
        self.preparation_installation_sha256 = preparation_installation_sha256
        self.route = route
        self.lock_path, self.ledger_path = Path(lock_path), Path(ledger_path)
        if not self.lock_path.is_absolute() or not self.ledger_path.is_absolute():
            raise ValueError('absolute local execution/ownership paths required')
        self.fd = None

    def acquire(self):
        if self.fd is not None:
            raise RuntimeError('execution gate already acquired')
        owner_uid = os.getuid()
        if self.trusted_operator_uid is not None:
            # Only the explicit privileged gripper entry may share lab's lock.
            # Do not trust environment variables or accept arbitrary target UIDs.
            if os.geteuid() != 0 or owner_uid != 0:
                raise PermissionError('only root workers may select the fixed lab operator UID')
            if self.trusted_operator_uid != pwd.getpwnam('lab').pw_uid:
                raise PermissionError('trusted operator UID must match the fixed lab account')
            owner_uid = self.trusted_operator_uid
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        # Existing files must not use O_CREAT: Linux protected_regular blocks
        # root O_CREAT on lab-owned locks in sticky /tmp even when they exist.
        # A raced creation retries an ordinary open; never unlink/replace locks.
        fd = None
        created = False
        for _ in range(3):
            try:
                fd = os.open(str(self.lock_path), flags)
                break
            except FileNotFoundError:
                try:
                    fd = os.open(str(self.lock_path), flags | os.O_CREAT | os.O_EXCL, 0o600)
                    created = True
                    break
                except FileExistsError:
                    continue
        if fd is None:
            raise RuntimeError('execution lock changed repeatedly during acquisition')
        try:
            if created and owner_uid != os.geteuid():
                os.fchown(fd, owner_uid, -1)
            _private_regular(fd, 'arm execution lock', owner_uid)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError('another SSH/network arm execution still owns this robot') from exc
            owner = _network_owner(self.ledger_path, owner_uid)
            if self.route == 'ssh':
                if owner is not None and owner['active']:
                    raise RuntimeError('network session cleanup is unconfirmed; SSH takeover refused')
            elif self.route == 'preparation':
                if (owner is None or not owner['active'] or owner.get('operation') != 'preparation'
                        or owner.get('request_id') != self.preparation_request_id
                        or owner.get('installation_sha256') != self.preparation_installation_sha256
                        or not self._supervisor_is_parent(owner['supervisor_pid'], owner_uid)):
                    raise RuntimeError('preparation helper lacks its exact parent reservation')
            elif (owner is None or not owner['active'] or owner.get('operation') == 'preparation'
                  or not self._supervisor_is_parent(owner['supervisor_pid'], owner_uid)):
                raise RuntimeError('network worker lacks an active reservation from its parent service')
            self.fd = fd
        except BaseException:
            os.close(fd)
            raise
        return self

    def _supervisor_is_parent(self, expected, owner_uid):
        if self.trusted_operator_uid is None:
            return expected == os.getppid()
        # sudo may insert a monitor process. Only a real ancestor with lab's
        # UID can own this reservation; never accept a caller-provided PID.
        pid = os.getppid()
        for _ in range(8):
            if pid <= 1:
                return False
            path = Path('/proc') / str(pid)
            try:
                if pid == expected:
                    return path.stat().st_uid == owner_uid
                pid = int((path/'stat').read_text().rsplit(')', 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                return False
        return False

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback):
        self.close()
