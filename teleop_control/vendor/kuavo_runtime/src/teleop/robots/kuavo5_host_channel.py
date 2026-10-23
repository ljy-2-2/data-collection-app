"""Host session I/O, separate from input policy and robot control.

Importing or constructing a channel performs no device I/O. Only ``open`` starts
the supplied process. This module is host-only and is not in the robot bundle.
"""

import json
import os
import select
import subprocess
import threading
import time
from typing import Iterator, Protocol

from teleop.robots.kuavo5_live_protocol import decode_robot_packet


class HostSessionChannel(Protocol):
    """One host session; receive and stderr readers may run on separate threads."""

    def open(self, bundle: dict) -> None: ...
    def receive(self) -> dict | None: ...
    def stderr_lines(self) -> Iterator[str]: ...
    def send_input(self, packet: dict, *, timeout_s: float,
                   absolute_deadline: float | None = None) -> None: ...
    def close_input(self) -> None: ...
    def poll(self) -> int | None: ...
    def finish(self) -> None: ...


def write_packet(writer, packet, *, timeout_s, absolute_deadline=None):
    """Bound pipe backpressure, including the initial in-memory bundle upload."""
    payload = memoryview((json.dumps(packet, allow_nan=False)+'\n').encode())
    fd = writer.fileno()
    os.set_blocking(fd, False)
    deadline = time.monotonic()+timeout_s
    if absolute_deadline is not None:
        deadline = min(deadline, absolute_deadline)
    while payload:
        remaining = deadline-time.monotonic()
        if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
            raise TimeoutError('SSH input pipe blocked; robot lease is not renewed')
        # select() returning writable is not a time guarantee.  Do not let a
        # scheduling pause between select and os.write cross the hard deadline.
        if time.monotonic() >= deadline:
            raise TimeoutError('SSH input pipe blocked; robot lease is not renewed')
        try:
            count = os.write(fd, payload)
        except BlockingIOError:
            continue
        payload = payload[count:]


class SSHSessionChannel:
    """The existing SSH subprocess and newline JSON wire format.

    Command construction stays with the caller. No fallback connection or
    process termination is added here; EOF closes only this session's input.
    """

    def __init__(self, command, *, process_factory=subprocess.Popen,
                 packet_writer=write_packet):
        self._command = command
        self._process_factory = process_factory
        self._packet_writer = packet_writer
        self._process = None
        self._open_attempted = False
        self._input_closed = False
        self._stdout_closed = False
        self._stderr_closed = False
        self._stdout_lock = threading.Lock()
        self._stderr_lock = threading.Lock()

    def open(self, bundle):
        if self._open_attempted:
            raise RuntimeError('host channel can only be opened once')
        self._open_attempted = True
        # Terminal Ctrl+C belongs to the host policy: it stops new input and
        # closes stdin so the remote session can return its shutdown report.
        # Do not let the same foreground-group SIGINT kill SSH before that
        # report arrives. This neither ignores host interrupts nor changes
        # robot leases, shutdown deadlines, or the shared SSH master.
        self._process = self._process_factory(
            self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True)
        self.send_input(bundle, timeout_s=5.)

    def _require_process(self):
        if self._process is None:
            raise RuntimeError('host channel is not open')
        return self._process

    def receive(self):
        with self._stdout_lock:
            if self._stdout_closed:
                return None
            line = self._require_process().stdout.readline(65538)
            if not line:
                return None
            if len(line) > 65536:
                raise ValueError('robot response too large')
            return decode_robot_packet(line)

    def stderr_lines(self):
        with self._stderr_lock:
            if not self._stderr_closed:
                yield from self._require_process().stderr

    def send_input(self, packet, *, timeout_s, absolute_deadline=None):
        process = self._require_process()
        if self._input_closed:
            raise ValueError('host channel input is closed')
        kwargs = dict(timeout_s=timeout_s)
        if absolute_deadline is not None:
            kwargs['absolute_deadline'] = absolute_deadline
        self._packet_writer(process.stdin, packet, **kwargs)

    def close_input(self):
        if self._process is not None and not self._input_closed:
            self._input_closed = True
            try:
                self._process.stdin.close()
            except (OSError, ValueError):
                pass

    def poll(self):
        return None if self._process is None else self._process.poll()

    def finish(self):
        """Close completed local resources without waiting for or killing a process.

        An active reader retains its stream. Closing a stream under a blocked
        reader could otherwise hang cleanup or discard an exit report.
        """
        if self._process is None or self.poll() is None:
            return
        self.close_input()
        for name, lock in (('stdout', self._stdout_lock), ('stderr', self._stderr_lock)):
            if not lock.acquire(blocking=False):
                continue
            try:
                if not getattr(self, '_'+name+'_closed'):
                    getattr(self._process, name).close()
                    setattr(self, '_'+name+'_closed', True)
            finally:
                lock.release()
