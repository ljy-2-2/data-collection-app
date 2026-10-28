"""Installation-bound runtime settings, separate from immutable robot releases.

Reading never writes unless explicitly constructed with create_if_missing=True.
Updates use revision compare-and-swap under a local lock, then atomic replace and
fsync. A damaged existing document is never interpreted as a fresh installation.
No ROS, motor access, or network operations belong in this module.
"""
import copy
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat

from .kuavo5_service_contract import expand_settings_changes
from .kuavo5_session_settings import (make_snapshot, settings_defaults,
                                    validate_snapshot)

DEFAULT_DIRECTORY = Path('/var/lib/galaxis-kuavo-network/runtime-settings')
_MAX_BYTES = 32768
_FORMAT = 'kuavo_session_settings_store_v1'


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate runtime settings field')
        result[key] = value
    return result


class SettingsStore:
    """One private parameter file per exact formal installation.

    describe() returns {snapshot, last_request_id}; load() returns the snapshot.
    preview(changes, expected_revision) validates without writing.
    apply/reset return the committed snapshot. Callers reconcile an uncertain
    reply using describe().last_request_id, never by automatically resubmitting.
    """
    def __init__(self, bundle, installation_sha256, *, directory=DEFAULT_DIRECTORY,
                 owner_uid=None, create_if_missing=False):
        if (not isinstance(installation_sha256, str)
                or re.fullmatch('[a-f0-9]{64}', installation_sha256) is None):
            raise ValueError('exact installation identity required')
        if type(create_if_missing) is not bool:
            raise ValueError('explicit initialization boolean required')
        self.bundle = copy.deepcopy(bundle)
        self.installation_sha256 = installation_sha256
        self.directory = Path(directory)
        if not self.directory.is_absolute():
            raise ValueError('absolute runtime settings directory required')
        self.owner_uid = os.getuid() if owner_uid is None else owner_uid
        if type(self.owner_uid) is not int or self.owner_uid < 0:
            raise ValueError('valid settings owner UID required')
        self.create_if_missing = create_if_missing
        self.filename = installation_sha256 + '.json'
        self.path = self.directory / self.filename
        self.defaults = settings_defaults(self.bundle)
        # Validate the installation and defaults before touching any file.
        make_snapshot(self.bundle, installation_sha256, 0, self.defaults)

    def _private(self, fd, directory=False):
        info = os.fstat(fd)
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if (not expected(info.st_mode) or info.st_uid != self.owner_uid
                or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
                or (not directory and info.st_nlink != 1)):
            raise ValueError('runtime settings require private owned directory/file (0700/0600)')

    @contextmanager
    def _directory(self):
        fd = os.open(str(self.directory), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            self._private(fd, directory=True)
            yield fd
        finally:
            os.close(fd)

    def _read(self, directory_fd):
        fd = os.open(self.filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=directory_fd)
        try:
            self._private(fd)
            chunks, length = [], 0
            while True:
                chunk = os.read(fd, _MAX_BYTES+1-length)
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
                if length > _MAX_BYTES:
                    raise ValueError('runtime settings file too large')
            value = json.loads(b''.join(chunks), object_pairs_hook=_unique)
        finally:
            os.close(fd)
        if (not isinstance(value, dict) or set(value) != {'format', 'snapshot', 'last_request_id'}
                or value['format'] != _FORMAT):
            raise ValueError('invalid runtime settings document')
        request_id = value['last_request_id']
        if request_id is not None and (not isinstance(request_id, str)
                or re.fullmatch('[a-f0-9]{32}', request_id) is None):
            raise ValueError('invalid runtime settings request receipt')
        snapshot = validate_snapshot(self.bundle, self.installation_sha256, value['snapshot'])
        if (snapshot['revision'] == 0) != (request_id is None):
            raise ValueError('runtime settings revision and request receipt disagree')
        return dict(snapshot=snapshot, last_request_id=request_id)

    @contextmanager
    def _write_lock(self, directory_fd):
        name = self.installation_sha256+'.lock'
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
        except FileExistsError:
            fd = os.open(name, flags, dir_fd=directory_fd)
        try:
            self._private(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError('another runtime settings update is in progress') from exc
            yield
        finally:
            os.close(fd)

    def _write(self, directory_fd, snapshot, request_id):
        document = dict(format=_FORMAT, snapshot=snapshot, last_request_id=request_id)
        data = (json.dumps(document, sort_keys=True, allow_nan=False)+'\n').encode()
        if len(data) > _MAX_BYTES:
            raise ValueError('runtime settings file too large')
        name = '.'+self.installation_sha256+'.'+secrets.token_hex(16)+'.tmp'
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory_fd)
        try:
            self._private(fd)
            pending = memoryview(data)
            while pending:
                count = os.write(fd, pending)
                if count <= 0:
                    raise OSError('incomplete runtime settings write')
                pending = pending[count:]
            os.fsync(fd)
            os.replace(name, self.filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(fd)
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass

    def describe(self):
        with self._directory() as directory_fd:
            try:
                result = self._read(directory_fd)
                self.create_if_missing = False
                return result
            except FileNotFoundError:
                if not self.create_if_missing:
                    raise
            with self._write_lock(directory_fd):
                try:
                    result = self._read(directory_fd)
                except FileNotFoundError:
                    snapshot = make_snapshot(self.bundle, self.installation_sha256, 0, self.defaults)
                    self._write(directory_fd, snapshot, None)
                    result = dict(snapshot=snapshot, last_request_id=None)
                self.create_if_missing = False
                return result

    def load(self):
        return self.describe()['snapshot']

    def _candidate(self, current, changes, expected_revision):
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('expected_revision must be a nonnegative integer')
        if current['revision'] != expected_revision:
            raise ValueError('settings revision changed; read and review current values')
        changes = expand_settings_changes(changes, self.defaults)
        values = dict(current['values'])
        values.update(changes)
        return make_snapshot(self.bundle, self.installation_sha256, expected_revision+1, values)

    def preview(self, changes, expected_revision):
        return self._candidate(self.load(), changes, expected_revision)

    def apply(self, changes, expected_revision, request_id):
        if not isinstance(request_id, str) or re.fullmatch('[a-f0-9]{32}', request_id) is None:
            raise ValueError('exact runtime settings request identity required')
        with self._directory() as directory_fd, self._write_lock(directory_fd):
            current = self._read(directory_fd)
            if current['last_request_id'] == request_id:
                raise ValueError('settings request already committed; inspect its receipt')
            snapshot = self._candidate(current['snapshot'], changes, expected_revision)
            self._write(directory_fd, snapshot, request_id)
            return snapshot

    def reset(self, expected_revision, request_id):
        return self.apply(dict(self.defaults), expected_revision, request_id)
