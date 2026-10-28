"""Private PC request receipts; no network or robot I/O.

Persist a request identity before dispatch. An interrupted request stays pending
until its existing result is observed; this store never retries an operation.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat


_ACTIONS = frozenset(('start', 'initialize', 'grippers_inspect', 'grippers_prepare'))
_IDENTITY = {'service_instance_id': 32, 'installation_sha256': 64, 'bundle_sha256': 64}
_FIELDS = frozenset(('version', 'status', 'action', 'request_id', *_IDENTITY))


def _hex(value, size):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{%d}' % size, value) is not None


def _record(value):
    if (not isinstance(value, dict) or set(value) != _FIELDS
            or type(value['version']) is not int or value['version'] != 1
            or value['status'] not in ('pending', 'resolved')
            or not isinstance(value['action'], str) or value['action'] not in _ACTIONS
            or not _hex(value['request_id'], 32)
            or any(not _hex(value[key], size) for key, size in _IDENTITY.items())):
        raise ValueError('invalid wizard request receipt; do not repeat the action')
    return dict(value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate wizard receipt field')
        result[key] = value
    return result


class ReceiptStore:
    """Use as a context manager around one entire wizard invocation."""
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self._directory_fd = None
        self._lock_fd = None

    @staticmethod
    def _check_file(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError('wizard receipt file must be private, regular and owned by this user')

    def acquire(self):
        if self._directory_fd is not None:
            raise RuntimeError('wizard receipt store is already acquired')
        self.state_dir.mkdir(mode=0o700, exist_ok=True)
        try:
            self._directory_fd = os.open(str(self.state_dir), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(self._directory_fd)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise ValueError('wizard state directory must be private and owned by this user')
            self._lock_fd = os.open('wizard.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                                    0o600, dir_fd=self._directory_fd)
            self._check_file(os.fstat(self._lock_fd))
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError('another PC wizard is already running') from exc
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        # Closing the descriptor releases flock, including after exceptions.
        for attribute in ('_lock_fd', '_directory_fd'):
            fd = getattr(self, attribute)
            if fd is not None:
                setattr(self, attribute, None)
                os.close(fd)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *unused):
        self.close()

    def _require_lock(self):
        if self._directory_fd is None or self._lock_fd is None:
            raise RuntimeError('acquire wizard receipt store before accessing records')

    def _read(self, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._directory_fd)
        try:
            self._check_file(os.fstat(fd))
            with os.fdopen(fd, 'r', encoding='utf-8') as stream:
                fd = None
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError('wizard receipt is too large')
            value = _record(json.loads(raw, object_pairs_hook=_unique_object))
            if name != 'request-' + value['request_id'] + '.json':
                raise ValueError('wizard receipt filename does not match its request identity')
            return value
        finally:
            if fd is not None:
                os.close(fd)

    def _replace(self, name, value):
        temporary = '.receipt-' + secrets.token_hex(16) + '.tmp'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self._directory_fd)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(value, stream, sort_keys=True, allow_nan=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=self._directory_fd, dst_dir_fd=self._directory_fd)
            os.fsync(self._directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self._directory_fd)
            except FileNotFoundError:
                pass

    def pending(self):
        self._require_lock()
        pending = []
        for name in sorted(os.listdir(self._directory_fd)):
            if name.startswith('request-'):
                value = self._read(name)
                if value['status'] == 'pending':
                    pending.append(value)
        if len(pending) > 1:
            raise ValueError('multiple pending wizard requests; inspect records before continuing')
        return pending[0] if pending else None

    def begin(self, action, identity):
        self._require_lock()
        if (not isinstance(action, str) or action not in _ACTIONS
                or not isinstance(identity, dict) or set(identity) != set(_IDENTITY)
                or any(not _hex(identity[key], size) for key, size in _IDENTITY.items())):
            raise ValueError('valid preparation action and exact public service identity required')
        if self.pending() is not None:
            raise ValueError('a pending wizard request must be observed before another action')
        value = _record(dict(version=1, status='pending', action=action,
                             request_id=secrets.token_hex(16), **identity))
        name = 'request-' + value['request_id'] + '.json'
        try:
            os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError('wizard request ID already exists')
        self._replace(name, value)
        return dict(value)

    def resolve(self, record):
        self._require_lock()
        value = _record(record)
        if value['status'] != 'pending':
            raise ValueError('only a pending wizard receipt can be resolved')
        # Validate every stored record first; never hide an ambiguous pending set.
        self.pending()
        name = 'request-' + value['request_id'] + '.json'
        current = self._read(name)
        if dict(current, status='pending') != value:
            raise ValueError('wizard request identity changed; cannot resolve a different request')
        if current['status'] == 'resolved':
            return current
        resolved = dict(value, status='resolved')
        self._replace(name, resolved)
        return resolved
