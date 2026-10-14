"""Small, latest-frame-only desktop previews, independent of ROS recording."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from io import BytesIO
import re

import cv2
import httpx
import numpy as np
from PIL import Image

PREVIEW_SIZE = (960, 768)
PREVIEW_QUALITY = 90
PREVIEW_FPS = 24
MAX_FRAME_BYTES = 8 * 1024 * 1024
BOUNDARY = b'boundarydonotcross'


class MjpegFrames:
    """Parse complete multipart JPEGs, including embedded thumbnail markers."""
    def __init__(self):
        self.buffer = bytearray()
        self.body_size = None

    def feed(self, chunk):
        self.buffer.extend(chunk)
        latest = None
        while True:
            if self.body_size is None:
                end = self.buffer.find(b'\r\n\r\n')
                if end < 0:
                    if len(self.buffer) > 16384:
                        raise ValueError('Invalid MJPEG header')
                    break
                header = bytes(self.buffer[:end])
                match = re.search(rb'(?im)^content-length:\s*(\d+)\s*$', header)
                if match is None:
                    raise ValueError('MJPEG frame has no Content-Length')
                self.body_size = int(match[1])
                if not 0 < self.body_size <= MAX_FRAME_BYTES:
                    raise ValueError('Invalid MJPEG frame size')
                del self.buffer[:end + 4]
            if len(self.buffer) < self.body_size:
                break
            latest = bytes(self.buffer[:self.body_size])
            del self.buffer[:self.body_size]
            self.body_size = None
        return latest


def preview_size(width: int, height: int) -> tuple[int, int]:
    """Fit the display copy without changing aspect ratio or enlarging it."""
    scale = min(1.0, PREVIEW_SIZE[0] / width, PREVIEW_SIZE[1] / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def preview_jpeg(jpeg: bytes) -> bytes:
    # Pillow reads only the header here. Native OpenCV decode/resize/encode
    # reduces conversion contention when three previews run concurrently on ARM.
    # The recorded JPEG, ROS parameters and source dimensions remain intact.
    with Image.open(BytesIO(jpeg)) as source:
        width, height = source.size
        target = preview_size(width, height)
    flags = cv2.IMREAD_COLOR
    for factor, flag in (
        (8, cv2.IMREAD_REDUCED_COLOR_8),
        (4, cv2.IMREAD_REDUCED_COLOR_4),
        (2, cv2.IMREAD_REDUCED_COLOR_2),
    ):
        if width // factor >= target[0] and height // factor >= target[1]:
            flags = flag
            break
    try:
        # Keep the camera's pixel orientation. EXIF must not rotate only the
        # preview while its corresponding recorded image stays unchanged.
        frame = cv2.imdecode(
            np.frombuffer(jpeg, np.uint8), flags | cv2.IMREAD_IGNORE_ORIENTATION
        )
        if frame is None:
            raise ValueError('Invalid preview JPEG')
        if (frame.shape[1], frame.shape[0]) != target:
            frame = cv2.resize(frame, target, interpolation=cv2.INTER_LINEAR)
        encoded, output = cv2.imencode(
            '.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_QUALITY]
        )
        if not encoded:
            raise ValueError('Could not encode preview JPEG')
        return output.tobytes()
    except cv2.error as exc:
        raise ValueError('Could not convert preview JPEG') from exc


def multipart(jpeg):
    return (b'--' + BOUNDARY + b'\r\nContent-Type: image/jpeg\r\nContent-Length: '
            + str(len(jpeg)).encode() + b'\r\n\r\n' + jpeg + b'\r\n')


async def preview_stream(upstream_url, *, clock=None, sleep=asyncio.sleep):
    # Keep consuming the source while encoding/sending one frame. A slow UI
    # replaces the pending frame, never queues seconds of old camera images.
    frames = asyncio.Queue(maxsize=1)
    clock = clock or asyncio.get_running_loop().time
    next_frame_at = 0.0

    def offer(value):
        if frames.full():
            frames.get_nowait()
        frames.put_nowait(value)

    async def receive():
        parser = MjpegFrames()
        cancelled = False
        try:
            timeout = httpx.Timeout(5.0, connect=3.0)
            async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=True) as client:
                async with client.stream('GET', upstream_url) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_raw():
                        latest = parser.feed(chunk)
                        if latest is not None:
                            offer(latest)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            # Preserve the last complete frame at EOF. Waiting here cannot
            # block the camera: its HTTP response has already been closed.
            # Cancellation must not wait for a disconnected viewer to drain
            # a full queue of pending display frames.
            if not cancelled:
                await frames.put(None)

    receiver = asyncio.create_task(receive())
    try:
        while True:
            jpeg = await frames.get()
            if jpeg is None:
                await receiver
                break
            remaining = next_frame_at - clock()
            if remaining > 0:
                await sleep(remaining)
            finished = False
            # The source keeps running during pacing. Encode its newest frame,
            # never the older one taken before that wait.
            while not frames.empty():
                newest = frames.get_nowait()
                if newest is None:
                    finished = True
                    break
                jpeg = newest
            # Limit work BEFORE JPEG decoding/encoding, not just HTTP output.
            # A slow conversion or client never builds a catch-up backlog.
            next_frame_at = clock() + 1.0 / PREVIEW_FPS
            try:
                reduced = await asyncio.to_thread(preview_jpeg, jpeg)
            except (OSError, ValueError):
                if finished:
                    await receiver
                    break
                continue
            yield multipart(reduced)
            if finished:
                await receiver
                break
    finally:
        receiver.cancel()
        with suppress(asyncio.CancelledError):
            await receiver
