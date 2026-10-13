"""Read G2 kHeadColor JPEG frames and forward them without robot commands."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib
import math
import operator
from pathlib import Path
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable

from teleop.vision import VideoFrame, VideoStreamCapability, send_frame


G2_HEAD_CAMERA = "kHeadColor"
G2_HEAD_WIDTH = 640
G2_HEAD_HEIGHT = 400


@dataclass(frozen=True)
class G2CameraCapability:
    width: int
    height: int
    declared_fps: float

    def __post_init__(self) -> None:
        _positive_integer(self.width, "camera capability width")
        _positive_integer(self.height, "camera capability height")
        _positive_finite(self.declared_fps, "camera capability fps")


class G2VideoSource:
    """Read-only G2 implementation of the robot-independent video contract."""

    def __init__(
        self,
        *,
        gdk: object,
        reader_factory: Callable[..., object] | None = None,
        camera_name: str = G2_HEAD_CAMERA,
        settle_seconds: float = 2.0,
        timeout_ms: float = 250.0,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if camera_name != G2_HEAD_CAMERA:
            raise ValueError(f"version 1 supports only {G2_HEAD_CAMERA}")
        _nonnegative_finite(settle_seconds, "settle_seconds")
        _positive_finite(timeout_ms, "timeout_ms")
        self._gdk = gdk
        self._camera_name = camera_name
        self._clock_ns = clock_ns
        self._closed = False
        self._previous_source_timestamp_ns: int | None = None
        self._next_sequence = 0
        self._success = gdk.GDKRes.kSuccess  # type: ignore[attr-defined]
        if gdk.gdk_init() != self._success:  # type: ignore[attr-defined]
            raise RuntimeError("GDK initialization failed; camera reader was not created")

        try:
            robot = gdk.Robot()  # type: ignore[attr-defined]
            camera = gdk.Camera()  # type: ignore[attr-defined]
            if settle_seconds:
                time.sleep(settle_seconds)
            camera_type = getattr(gdk.CameraType, camera_name)  # type: ignore[attr-defined]
            capability = probe_g2_camera_capability(camera, camera_type)
            self._capabilities = (
                VideoStreamCapability(
                    camera_name=camera_name,
                    width=capability.width,
                    height=capability.height,
                    encoding="jpeg",
                    nominal_fps=capability.declared_fps,
                ),
            )
            factory = reader_factory or _default_reader_factory
            self._reader = factory(
                gdk=gdk,
                robot=robot,
                camera=camera,
                camera_name=camera_name,
                timeout_ms=timeout_ms,
                clock_ns=clock_ns,
            )
        except BaseException as primary_error:
            try:
                release_result = gdk.gdk_release()  # type: ignore[attr-defined]
            except BaseException as release_error:
                print(
                    f"WARNING: GDK release also failed while handling "
                    f"{primary_error!r}: {release_error!r}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                if release_result != self._success:
                    print(
                        f"WARNING: GDK release returned {release_result!r} while "
                        f"handling {primary_error!r}",
                        file=sys.stderr,
                        flush=True,
                    )
            self._closed = True
            raise

    @property
    def capabilities(self) -> tuple[VideoStreamCapability, ...]:
        return self._capabilities

    def read_latest_frame(self, *, timeout_s: float) -> VideoFrame | None:
        if self._closed:
            raise RuntimeError("G2 video source is closed")
        _positive_finite(timeout_s, "timeout_s")
        encoded = self._reader.read_encoded_camera(  # type: ignore[attr-defined]
            self._camera_name, timeout_ms=timeout_s * 1000.0
        )
        capability = self._capabilities[0]
        if encoded.width != capability.width or encoded.height != capability.height:
            raise RuntimeError(
                f"{self._camera_name} must be {capability.width}x{capability.height}; "
                f"got {encoded.width}x{encoded.height}"
            )
        if self._previous_source_timestamp_ns is not None:
            if encoded.timestamp_ns < self._previous_source_timestamp_ns:
                raise RuntimeError(
                    f"{self._camera_name} source timestamp moved backwards"
                )
            if encoded.timestamp_ns == self._previous_source_timestamp_ns:
                return None
        frame = VideoFrame(
            camera_name=self._camera_name,
            sequence_id=self._next_sequence,
            source_timestamp_ns=encoded.timestamp_ns,
            pc_received_monotonic_ns=self._clock_ns(),
            width=encoded.width,
            height=encoded.height,
            encoding="jpeg",
            payload=encoded.jpeg,
        )
        self._previous_source_timestamp_ns = encoded.timestamp_ns
        self._next_sequence += 1
        return frame

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        release_result = self._gdk.gdk_release()  # type: ignore[attr-defined]
        if release_result != self._success:
            raise RuntimeError(f"GDK release failed: {release_result!r}")


@dataclass(frozen=True)
class G2CameraStreamSummary:
    frames_sent: int
    capability: G2CameraCapability
    duplicate_frames_skipped: int
    total_payload_bytes: int
    first_source_timestamp_ns: int | None
    last_source_timestamp_ns: int | None
    first_pc_received_ns: int | None
    last_pc_received_ns: int | None
    min_source_interval_ns: int | None = None
    max_source_interval_ns: int | None = None
    physical_robot_command_sent: bool = False

    @property
    def average_payload_bytes(self) -> float:
        if self.frames_sent == 0:
            return 0.0
        return self.total_payload_bytes / self.frames_sent

    @property
    def measured_source_fps(self) -> float | None:
        return _measured_fps(
            self.frames_sent,
            self.first_source_timestamp_ns,
            self.last_source_timestamp_ns,
        )

    @property
    def measured_delivery_fps(self) -> float | None:
        return _measured_fps(
            self.frames_sent,
            self.first_pc_received_ns,
            self.last_pc_received_ns,
        )


def probe_g2_camera_capability(
    camera: object, camera_type: object
) -> G2CameraCapability:
    """Read only the dimensions and FPS advertised by the GDK camera API."""

    try:
        raw_shape = camera.get_image_shape(camera_type)  # type: ignore[attr-defined]
    except Exception as error:
        raise RuntimeError(f"camera shape query failed: {error}") from error
    try:
        shape = tuple(raw_shape)
    except TypeError as error:
        raise ValueError("camera shape must contain width and height") from error
    if len(shape) != 2:
        raise ValueError("camera shape must contain exactly width and height")
    width = _positive_integer(shape[0], "camera shape width")
    height = _positive_integer(shape[1], "camera shape height")

    try:
        raw_fps = camera.get_image_fps(camera_type)  # type: ignore[attr-defined]
    except Exception as error:
        raise RuntimeError(f"camera fps query failed: {error}") from error
    declared_fps = _positive_finite(raw_fps, "camera fps")
    return G2CameraCapability(
        width=width,
        height=height,
        declared_fps=declared_fps,
    )


def stream_real_g2_camera(
    *,
    gdk: object,
    send_frame: Callable[[VideoFrame], None],
    reader_factory: Callable[..., object] | None = None,
    camera_name: str = G2_HEAD_CAMERA,
    max_frames: int | None = None,
    duration_s: float | None = None,
    max_fps: float = 15.0,
    settle_seconds: float = 2.0,
    timeout_ms: float = 250.0,
    stop_requested: Callable[[], bool] = lambda: False,
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> G2CameraStreamSummary:
    """Own one read-only GDK lifecycle and forward validated JPEG frames."""

    if camera_name != G2_HEAD_CAMERA:
        raise ValueError(f"version 1 supports only {G2_HEAD_CAMERA}")
    _positive_finite(max_fps, "max_fps")
    _positive_finite(timeout_ms, "timeout_ms")
    _nonnegative_finite(settle_seconds, "settle_seconds")
    if duration_s is not None:
        _positive_finite(duration_s, "duration_s")
    if max_frames is not None and (
        isinstance(max_frames, bool) or not isinstance(max_frames, int) or max_frames < 1
    ):
        raise ValueError("max_frames must be a positive integer or None")

    primary_error: BaseException | None = None
    source: G2VideoSource | None = None
    frames_sent = 0
    duplicate_frames_skipped = 0
    total_payload_bytes = 0
    first_source_timestamp_ns: int | None = None
    last_source_timestamp_ns: int | None = None
    first_pc_received_ns: int | None = None
    last_pc_received_ns: int | None = None
    min_source_interval_ns: int | None = None
    max_source_interval_ns: int | None = None
    try:
        source = G2VideoSource(
            gdk=gdk,
            reader_factory=reader_factory,
            camera_name=camera_name,
            settle_seconds=settle_seconds,
            timeout_ms=timeout_ms,
            clock_ns=clock_ns,
        )
        stream_capability = source.capabilities[0]
        capability = G2CameraCapability(
            width=stream_capability.width,
            height=stream_capability.height,
            declared_fps=stream_capability.nominal_fps,
        )

        started_ns = clock_ns()
        deadline_ns = (
            None if duration_s is None else started_ns + round(duration_s * 1e9)
        )
        period_ns = round(1e9 / max_fps)
        next_frame_ns = started_ns
        previous_source_timestamp_ns: int | None = None
        while not stop_requested():
            now_ns = clock_ns()
            if deadline_ns is not None and now_ns >= deadline_ns:
                break
            if max_frames is not None and frames_sent >= max_frames:
                break
            if now_ns < next_frame_ns:
                time.sleep((next_frame_ns - now_ns) / 1e9)
                continue

            frame = source.read_latest_frame(timeout_s=timeout_ms / 1000.0)
            if frame is None:
                duplicate_frames_skipped += 1
                next_frame_ns = max(next_frame_ns + period_ns, clock_ns())
                continue
            if previous_source_timestamp_ns is not None:
                source_interval_ns = (
                    frame.source_timestamp_ns - previous_source_timestamp_ns
                )
                min_source_interval_ns = (
                    source_interval_ns
                    if min_source_interval_ns is None
                    else min(min_source_interval_ns, source_interval_ns)
                )
                max_source_interval_ns = (
                    source_interval_ns
                    if max_source_interval_ns is None
                    else max(max_source_interval_ns, source_interval_ns)
                )
            send_frame(frame)
            frames_sent += 1
            total_payload_bytes += len(frame.payload)
            if first_source_timestamp_ns is None:
                first_source_timestamp_ns = frame.source_timestamp_ns
                first_pc_received_ns = frame.pc_received_monotonic_ns
            last_source_timestamp_ns = frame.source_timestamp_ns
            last_pc_received_ns = frame.pc_received_monotonic_ns
            previous_source_timestamp_ns = frame.source_timestamp_ns
            next_frame_ns = max(next_frame_ns + period_ns, clock_ns())
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if source is not None:
            try:
                source.close()
            except BaseException as release_error:
                if primary_error is None:
                    raise RuntimeError("GDK release failed") from release_error
                print(
                    f"WARNING: GDK release also failed while handling "
                    f"{primary_error!r}: {release_error!r}",
                    file=sys.stderr,
                    flush=True,
                )

    return G2CameraStreamSummary(
        frames_sent=frames_sent,
        capability=capability,
        duplicate_frames_skipped=duplicate_frames_skipped,
        total_payload_bytes=total_payload_bytes,
        first_source_timestamp_ns=first_source_timestamp_ns,
        last_source_timestamp_ns=last_source_timestamp_ns,
        first_pc_received_ns=first_pc_received_ns,
        last_pc_received_ns=last_pc_received_ns,
        min_source_interval_ns=min_source_interval_ns,
        max_source_interval_ns=max_source_interval_ns,
    )


def _default_reader_factory(
    *, gdk, robot, camera, camera_name, timeout_ms, clock_ns
):
    module = importlib.import_module("g2_dp.gdk_observation_reader")

    def unexpected_decode(_image):
        raise RuntimeError("pixel decoding is not used by the JPEG video bridge")

    return module.GDKObservationReader(
        robot=robot,
        camera=camera,
        camera_types={camera_name: getattr(gdk.CameraType, camera_name)},
        image_decoder=unexpected_decode,
        timeout_ms=timeout_ms,
        clock_ns=clock_ns,
    )


def _connect_unix(path: Path, timeout_s: float = 5.0) -> socket.socket:
    deadline = time.monotonic() + timeout_s
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.connect(str(path))
            connection.settimeout(1.0)
            return connection
        except OSError as error:
            last_error = error
            connection.close()
            time.sleep(0.05)
    raise RuntimeError(f"unable to connect to video bridge socket {path}: {last_error}")


def run(args: argparse.Namespace) -> int:
    stop = threading.Event()

    def request_stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    connection: socket.socket | None = None
    print(
        "G2_CAMERA_WORKER_PRECHECK mode=live-read-only camera=kHeadColor "
        "physical_robot_command_sent=False",
        flush=True,
    )
    try:
        connection = _connect_unix(args.socket_path)
        gdk = importlib.import_module("agibot_gdk")
        summary = stream_real_g2_camera(
            gdk=gdk,
            send_frame=lambda frame: send_frame(connection, frame),
            max_frames=args.max_frames,
            duration_s=args.duration,
            max_fps=args.fps,
            settle_seconds=args.settle_seconds,
            timeout_ms=args.timeout_ms,
            stop_requested=stop.is_set,
        )
    except Exception as error:
        print(
            f"G2_CAMERA_WORKER_ERROR error={error!r} "
            "physical_robot_command_sent=False",
            file=sys.stderr,
            flush=True,
        )
        return 2
    finally:
        if connection is not None:
            connection.close()
    print(
        f"G2_CAMERA_CAPABILITY camera={G2_HEAD_CAMERA} "
        f"width={summary.capability.width} height={summary.capability.height} "
        f"declared_fps={summary.capability.declared_fps:.3f} "
        "physical_robot_command_sent=False",
        flush=True,
    )
    print(
        f"G2_CAMERA_WORKER_SUMMARY frames_sent={summary.frames_sent} "
        f"requested_fps={args.fps:.3f} "
        f"measured_source_fps={_format_optional(summary.measured_source_fps)} "
        f"measured_delivery_fps={_format_optional(summary.measured_delivery_fps)} "
        f"duplicates_skipped={summary.duplicate_frames_skipped} "
        f"average_jpeg_bytes={summary.average_payload_bytes:.1f} "
        f"source_interval_min_ms={_format_ns_as_ms(summary.min_source_interval_ns)} "
        f"source_interval_max_ms={_format_ns_as_ms(summary.max_source_interval_ns)} "
        "physical_robot_command_sent=False",
        flush=True,
    )
    return 0


def _positive_finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0.0:
        raise ValueError(f"{name} must be a positive finite number")
    return normalized


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        normalized = operator.index(value)
    except TypeError as error:
        raise ValueError(f"{name} must be a positive integer") from error
    if normalized <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return int(normalized)


def _measured_fps(
    frames: int, first_timestamp_ns: int | None, last_timestamp_ns: int | None
) -> float | None:
    if frames < 2 or first_timestamp_ns is None or last_timestamp_ns is None:
        return None
    elapsed_ns = last_timestamp_ns - first_timestamp_ns
    if elapsed_ns <= 0:
        return None
    return (frames - 1) * 1e9 / elapsed_ns


def _format_optional(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _format_ns_as_ms(value: int | None) -> str:
    return "n/a" if value is None else f"{value / 1e6:.3f}"


def _nonnegative_finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a non-negative finite number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0.0:
        raise ValueError(f"{name} must be a non-negative finite number")
    return normalized


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read G2 kHeadColor JPEG frames without sending robot commands."
    )
    parser.add_argument("--socket-path", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=None)
    parser.add_argument("--max-frames", type=int, default=None)
    parser.add_argument("--fps", type=float, default=15.0)
    parser.add_argument("--settle-seconds", type=float, default=2.0)
    parser.add_argument("--timeout-ms", type=float, default=250.0)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
