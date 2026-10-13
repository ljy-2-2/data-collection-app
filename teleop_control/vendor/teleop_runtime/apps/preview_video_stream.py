"""Display the PC VideoHub stream in a local browser without saving frames."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import sys
import threading
import webbrowser

from teleop.vision import (
    LatestFrameMailbox,
    build_preview_html,
    encode_multipart_jpeg,
    read_frame,
)
from teleop.vision.preview import MULTIPART_BOUNDARY


class PreviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        mailbox: LatestFrameMailbox,
        stop_requested: threading.Event,
    ) -> None:
        self.mailbox = mailbox
        self.stop_requested = stop_requested
        super().__init__(address, PreviewRequestHandler)


class PreviewRequestHandler(BaseHTTPRequestHandler):
    server: PreviewHTTPServer

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path in ("/", "/index.html"):
            self._send_page()
            return
        if self.path == "/stream.mjpg":
            self._send_stream()
            return
        self.send_error(404)

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send_page(self) -> None:
        body = build_preview_html(
            camera_name="kHeadColor",
            stream_path="/stream.mjpg",
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_stream(self) -> None:
        self.send_response(200)
        self.send_header(
            "Content-Type",
            f"multipart/x-mixed-replace; boundary={MULTIPART_BOUNDARY}",
        )
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()

        last_sequence = -1
        try:
            while not self.server.stop_requested.is_set():
                frame = self.server.mailbox.wait_for_newer(
                    last_sequence,
                    timeout=0.25,
                )
                if frame is None:
                    continue
                self.wfile.write(encode_multipart_jpeg(frame.payload))
                self.wfile.flush()
                last_sequence = frame.sequence_id
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def receive_frames(
    *,
    connection: socket.socket,
    mailbox: LatestFrameMailbox,
    stop_requested: threading.Event,
    frame_limit: int | None,
    errors: list[BaseException],
) -> None:
    received = 0
    try:
        while not stop_requested.is_set():
            if frame_limit is not None and received >= frame_limit:
                break
            frame = read_frame(connection)
            if frame is None:
                break
            mailbox.publish(frame)
            received += 1
    except BaseException as error:
        if not stop_requested.is_set():
            errors.append(error)
    finally:
        stop_requested.set()
        mailbox.close()


def preview(
    *,
    host: str,
    port: int,
    frame_limit: int | None,
    timeout_s: float,
    http_port: int,
    open_browser: bool,
) -> int:
    mailbox = LatestFrameMailbox()
    stop_requested = threading.Event()
    errors: list[BaseException] = []
    connection: socket.socket | None = None
    server: PreviewHTTPServer | None = None
    receiver: threading.Thread | None = None
    try:
        connection = socket.create_connection((host, port), timeout=timeout_s)
        connection.settimeout(timeout_s)
        server = PreviewHTTPServer(
            ("127.0.0.1", http_port),
            mailbox=mailbox,
            stop_requested=stop_requested,
        )
        receiver = threading.Thread(
            target=receive_frames,
            kwargs={
                "connection": connection,
                "mailbox": mailbox,
                "stop_requested": stop_requested,
                "frame_limit": frame_limit,
                "errors": errors,
            },
            name="video-preview-receiver",
            daemon=True,
        )
        receiver.start()
        preview_host, preview_port = server.server_address[:2]
        url = f"http://{preview_host}:{preview_port}/"
        print(
            f"VIDEO_PREVIEW_READY url={url} camera=kHeadColor "
            "stop_with_ctrl_c=True physical_robot_command_sent=False",
            flush=True,
        )
        if open_browser and not webbrowser.open(url, new=1):
            print(f"VIDEO_PREVIEW_OPEN_MANUALLY url={url}", flush=True)

        server.timeout = 0.25
        while not stop_requested.is_set():
            server.handle_request()
    except KeyboardInterrupt:
        print(
            "VIDEO_PREVIEW_STOP reason=user_request "
            "physical_robot_command_sent=False",
            flush=True,
        )
    except Exception as error:
        errors.append(error)
    finally:
        stop_requested.set()
        mailbox.close()
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        if server is not None:
            server.server_close()
        if receiver is not None:
            receiver.join(timeout=1.0)

    if errors:
        print(
            f"VIDEO_PREVIEW_ERROR error={errors[0]!r} "
            "physical_robot_command_sent=False",
            file=sys.stderr,
            flush=True,
        )
        return 2
    print(
        f"VIDEO_PREVIEW_SUMMARY frames_received={mailbox.published_count} "
        f"frames_overwritten={mailbox.overwritten_count} "
        "physical_robot_command_sent=False",
        flush=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Display G2 head-camera frames in a local PC browser."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=63902)
    parser.add_argument(
        "--frames",
        type=int,
        default=0,
        help="stop after this many frames; 0 keeps previewing until stopped",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=0,
        help="local browser port; 0 chooses a free port",
    )
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="print the local URL without opening it automatically",
    )
    args = parser.parse_args()
    for name, value in (("--port", args.port), ("--http-port", args.http_port)):
        if value < 0 or value > 65535 or (name == "--port" and value == 0):
            parser.error(f"{name} is outside its valid range")
    if args.frames < 0:
        parser.error("--frames must be zero or positive")
    if args.timeout <= 0.0:
        parser.error("--timeout must be positive")
    return preview(
        host=args.host,
        port=args.port,
        frame_limit=None if args.frames == 0 else args.frames,
        timeout_s=args.timeout,
        http_port=args.http_port,
        open_browser=not args.no_browser,
    )


if __name__ == "__main__":
    raise SystemExit(main())
