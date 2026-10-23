"""Host-side latest-frame hub from one Unix producer to many TCP viewers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import math
import socket
import stat
import threading
import time
from typing import Callable

from .mailbox import LatestFrameMailbox
from .protocol import ProtocolError, read_frame, send_frame


class VideoHub:
    """Fan out one newest camera frame to independent reconnecting viewers."""

    def __init__(
        self,
        *,
        unix_path: Path,
        tcp_host: str = "127.0.0.1",
        tcp_port: int = 63902,
        viewer_send_timeout_s: float = 0.25,
        max_frame_age_s: float | None = None,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self.unix_path = Path(unix_path)
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.viewer_send_timeout_s = viewer_send_timeout_s
        if max_frame_age_s is not None and (not math.isfinite(max_frame_age_s) or max_frame_age_s <= 0):
            raise ValueError("max_frame_age_s must be positive and finite")
        self.max_frame_age_s = max_frame_age_s
        self.on_event = on_event or (lambda _message: None)
        self.mailbox = LatestFrameMailbox()
        self._stop = threading.Event()
        self._unix_listener: socket.socket | None = None
        self._tcp_listener: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._active: set[socket.socket] = set()
        self._viewers: set[socket.socket] = set()
        self._active_lock = threading.Lock()
        self._next_sequence = 0
        self._started = False
        self._owns_unix_path = False

    @property
    def tcp_address(self) -> tuple[str, int]:
        if self._tcp_listener is None:
            raise RuntimeError("video bridge has not started")
        host, port = self._tcp_listener.getsockname()[:2]
        return str(host), int(port)

    @property
    def viewer_count(self) -> int:
        with self._active_lock:
            return len(self._viewers)

    def start(self) -> None:
        if self._started:
            raise RuntimeError("video bridge is already started")
        self._prepare_unix_path()
        unix_listener: socket.socket | None = None
        tcp_listener: socket.socket | None = None
        try:
            unix_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            tcp_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            unix_listener.bind(str(self.unix_path))
            self._owns_unix_path = True
            self.unix_path.chmod(0o600)
            unix_listener.listen(1)
            unix_listener.settimeout(0.25)

            tcp_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            tcp_listener.bind((self.tcp_host, self.tcp_port))
            tcp_listener.listen(8)
            tcp_listener.settimeout(0.25)
        except BaseException:
            if unix_listener is not None:
                unix_listener.close()
            if tcp_listener is not None:
                tcp_listener.close()
            self._remove_owned_unix_socket()
            raise

        self._unix_listener = unix_listener
        self._tcp_listener = tcp_listener
        self._started = True
        self._threads = [
            threading.Thread(
                target=self._producer_accept_loop,
                name="g2-video-producer",
                daemon=True,
            ),
            threading.Thread(
                target=self._viewer_accept_loop,
                name="pico-video-viewer",
                daemon=True,
            ),
        ]
        for thread in self._threads:
            thread.start()

    def close(self) -> None:
        self._stop.set()
        self.mailbox.close()
        for listener in (self._unix_listener, self._tcp_listener):
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    pass
        with self._active_lock:
            active = tuple(self._active)
        for connection in active:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=1.0)
        self._remove_owned_unix_socket()

    def _prepare_unix_path(self) -> None:
        if not self.unix_path.parent.is_dir():
            raise RuntimeError(
                f"Unix socket parent directory does not exist: {self.unix_path.parent}"
            )
        try:
            mode = self.unix_path.lstat().st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(mode):
            raise RuntimeError(
                f"refusing to remove existing path that is not a socket: {self.unix_path}"
            )
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.1)
        try:
            probe.connect(str(self.unix_path))
        except ConnectionRefusedError:
            self.unix_path.unlink()
        except FileNotFoundError:
            return
        except OSError as error:
            raise RuntimeError(
                f"cannot prove existing Unix socket is stale: {self.unix_path}: {error}"
            ) from error
        else:
            raise RuntimeError(f"Unix socket is already active: {self.unix_path}")
        finally:
            probe.close()

    def _remove_owned_unix_socket(self) -> None:
        if not self._owns_unix_path:
            return
        try:
            mode = self.unix_path.lstat().st_mode
        except FileNotFoundError:
            self._owns_unix_path = False
            return
        if stat.S_ISSOCK(mode):
            self.unix_path.unlink()
        self._owns_unix_path = False

    def _producer_accept_loop(self) -> None:
        listener = self._unix_listener
        assert listener is not None
        while not self._stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._register(connection)
            self.on_event("producer_connected")
            try:
                while not self._stop.is_set():
                    frame = read_frame(connection)
                    if frame is None:
                        break
                    outgoing = replace(frame, sequence_id=self._next_sequence)
                    self._next_sequence += 1
                    self.mailbox.publish(outgoing)
            except (OSError, ProtocolError, RuntimeError, ValueError) as error:
                if not self._stop.is_set():
                    self.on_event(f"producer_disconnected error={error}")
            finally:
                self._unregister_and_close(connection)

    def _viewer_accept_loop(self) -> None:
        listener = self._tcp_listener
        assert listener is not None
        while not self._stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._register(connection, viewer=True)
            viewer_thread = threading.Thread(
                target=self._serve_viewer,
                args=(connection,),
                name="video-viewer",
                daemon=True,
            )
            with self._active_lock:
                self._threads.append(viewer_thread)
            viewer_thread.start()

    def _serve_viewer(self, connection: socket.socket) -> None:
        try:
            connection.settimeout(self.viewer_send_timeout_s)
        except OSError:
            self._unregister_and_close(connection)
            return
        try:
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 128 * 1024)
        except OSError:
            pass
        self.on_event("viewer_connected")
        last_sequence = -1
        try:
            while not self._stop.is_set():
                frame = self.mailbox.wait_for_newer(last_sequence, timeout=0.25)
                if frame is None:
                    continue
                if (self.max_frame_age_s is not None and
                        (time.monotonic_ns() - frame.pc_received_monotonic_ns) / 1e9 > self.max_frame_age_s):
                    last_sequence = frame.sequence_id
                    continue  # do not replay an expired cache to a reconnecting viewer.
                send_frame(connection, frame)
                last_sequence = frame.sequence_id
        except (OSError, socket.timeout, ProtocolError) as error:
            if not self._stop.is_set():
                self.on_event(f"viewer_disconnected error={error}")
        finally:
            self._unregister_and_close(connection)

    def _register(self, connection: socket.socket, *, viewer: bool = False) -> None:
        with self._active_lock:
            self._active.add(connection)
            if viewer:
                self._viewers.add(connection)

    def _unregister_and_close(self, connection: socket.socket) -> None:
        with self._active_lock:
            self._active.discard(connection)
            self._viewers.discard(connection)
        try:
            connection.close()
        except OSError:
            pass


# Keep existing launchers and imports working while the clearer VideoHub name is
# adopted by new code.
VideoBridge = VideoHub
