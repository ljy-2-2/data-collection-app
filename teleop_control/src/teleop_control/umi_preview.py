"""Small, latest-frame-only desktop previews, independent of ROS recording."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from io import BytesIO
import re

import httpx
from PIL import Image

PREVIEW_SIZE = (960, 768)
PREVIEW_QUALITY = 90
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


def preview_jpeg(jpeg: bytes) -> bytes:
    # Decode at reduced JPEG resolution, then encode only the display copy.
    # No ROS parameters, source messages or files are modified here.
    with Image.open(BytesIO(jpeg)) as source:
        source.draft('RGB', PREVIEW_SIZE)
        source.thumbnail(PREVIEW_SIZE, Image.Resampling.BILINEAR)
        output = BytesIO()
        source.convert('RGB').save(output, 'JPEG', quality=PREVIEW_QUALITY)
        return output.getvalue()


def multipart(jpeg):
    return (b'--' + BOUNDARY + b'\r\nContent-Type: image/jpeg\r\nContent-Length: '
            + str(len(jpeg)).encode() + b'\r\n\r\n' + jpeg + b'\r\n')


async def preview_stream(upstream_url):
    # Keep consuming the source while encoding/sending one frame. A slow UI
    # replaces the pending frame, never queues seconds of old camera images.
    frames = asyncio.Queue(maxsize=1)

    def offer(value):
        if frames.full():
            frames.get_nowait()
        frames.put_nowait(value)

    async def receive():
        parser = MjpegFrames()
        try:
            timeout = httpx.Timeout(5.0, connect=3.0)
            async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=True) as client:
                async with client.stream('GET', upstream_url) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_raw():
                        latest = parser.feed(chunk)
                        if latest is not None:
                            offer(latest)
        finally:
            offer(None)

    receiver = asyncio.create_task(receive())
    try:
        while True:
            jpeg = await frames.get()
            if jpeg is None:
                await receiver
                break
            try:
                reduced = await asyncio.to_thread(preview_jpeg, jpeg)
            except (OSError, ValueError):
                continue
            yield multipart(reduced)
    finally:
        receiver.cancel()
        with suppress(asyncio.CancelledError):
            await receiver
