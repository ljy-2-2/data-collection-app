import asyncio
from io import BytesIO
from pathlib import Path
import sys
import threading

import httpx
from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'teleop_control/src'))
from teleop_control import umi_preview as preview


def jpeg(color, size=(1920,1080)):
    out=BytesIO();Image.new('RGB',size,color).save(out,'JPEG');return out.getvalue()


def test_fragmented_bursts_use_content_length_and_bound_memory():
    parser=preview.MjpegFrames()
    first=jpeg('red');last=jpeg('blue')
    stream=preview.multipart(first)+preview.multipart(last)
    assert parser.feed(stream[:12]) is None
    assert parser.feed(stream[12:])==last
    assert len(parser.buffer)<10
    # JPEG can contain embedded thumbnails with their own end markers.
    body=b'\xff\xd8thumbnail\xff\xd9rest\xff\xd9'
    assert parser.feed(preview.multipart(body))==body
    with pytest.raises(ValueError):preview.MjpegFrames().feed(b'Content-Length: 999999999\r\n\r\n')
    with pytest.raises(ValueError):preview.MjpegFrames().feed(b'x'*17000)


@pytest.mark.parametrize('limit,size,expected',[
    ((960,768),(1920,1080),(960,540)), ((960,768),(1280,1024),(960,768)),
    ((800,640),(1920,1080),(800,450)), ((800,640),(1280,1024),(800,640)),
    ((640,512),(1920,1080),(640,360)), ((640,512),(1280,1024),(640,512)),
    ((640,512),(320,240),(320,240)),
])
def test_only_preview_is_scaled_original_bytes_are_unchanged(monkeypatch,limit,size,expected):
    monkeypatch.setattr(preview, 'PREVIEW_SIZE', limit)
    source=jpeg('red',size);saved=bytes(source)
    output=preview.preview_jpeg(source)
    assert Image.open(BytesIO(output)).size==expected
    assert Image.open(BytesIO(source)).size==size and source==saved


def test_hd_preview_uses_native_half_size_jpeg_decode(monkeypatch):
    monkeypatch.setattr(preview, 'PREVIEW_SIZE', (960, 768))
    original = preview.cv2.imdecode
    decoded_sizes = []
    decode_flags = []

    def decode(source, flags):
        result = original(source, flags)
        decoded_sizes.append((result.shape[1], result.shape[0]))
        decode_flags.append(flags)
        return result

    monkeypatch.setattr(preview.cv2, 'imdecode', decode)
    output = preview.preview_jpeg(jpeg('red', (1920, 1080)))
    assert decoded_sizes == [(960, 540)]
    assert decode_flags == [preview.cv2.IMREAD_REDUCED_COLOR_2 | preview.cv2.IMREAD_IGNORE_ORIENTATION]
    assert Image.open(BytesIO(output)).size == (960, 540)


def test_smaller_center_preview_decodes_half_size_without_changing_recorded_frame(monkeypatch):
    monkeypatch.setattr(preview, 'PREVIEW_SIZE', (640, 512))
    monkeypatch.setattr(preview, 'PREVIEW_QUALITY', 82)
    original = preview.cv2.imdecode
    decoded_sizes = []
    def decode(source, flags):
        image = original(source, flags)
        decoded_sizes.append((image.shape[1], image.shape[0]))
        return image
    monkeypatch.setattr(preview.cv2, 'imdecode', decode)
    saved = jpeg('blue', (1280, 1024))
    unchanged = bytes(saved)
    display = preview.preview_jpeg(saved)
    assert decoded_sizes == [(640, 512)]
    assert Image.open(BytesIO(display)).size == (640, 512)
    assert Image.open(BytesIO(saved)).size == (1280, 1024) and saved == unchanged


def test_preview_preserves_pixel_orientation_and_rgb_colors_despite_exif():
    source = Image.new('RGB', (320, 240))
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
    boxes = [(0, 0, 160, 120), (160, 0, 320, 120),
             (0, 120, 160, 240), (160, 120, 320, 240)]
    for color, box in zip(colors, boxes):
        source.paste(color, box)
    exif = Image.Exif()
    exif[0x0112] = 6  # Metadata suggests rotation; saved camera pixels do not.
    encoded = BytesIO()
    source.save(encoded, 'JPEG', quality=95, exif=exif)
    recorded_jpeg = encoded.getvalue()

    with Image.open(BytesIO(preview.preview_jpeg(recorded_jpeg))) as display:
        assert display.size == (320, 240)
        for point, color in zip([(40, 40), (200, 40), (40, 180), (200, 180)], colors):
            assert all(abs(actual - expected) < 8
                       for actual, expected in zip(display.getpixel(point), color))
    assert encoded.getvalue() == recorded_jpeg


def corrupt_jpeg():
    # Invalid Huffman table: the header still has a valid size, but native JPEG
    # decoding must reject the image instead of crashing the preview stream.
    corrupted = bytearray(jpeg('red', (320, 240)))
    table = corrupted.index(b'\xff\xc4')
    corrupted[table + 4] = 255
    return bytes(corrupted)


def test_native_decoder_rejects_damaged_jpeg_with_valid_dimensions():
    damaged = corrupt_jpeg()
    assert Image.open(BytesIO(damaged)).size == (320, 240)
    with pytest.raises(ValueError, match='Invalid preview JPEG'):
        preview.preview_jpeg(damaged)


def test_opencv_error_becomes_a_skippable_preview_error(monkeypatch):
    def failure(*_args):
        raise preview.cv2.error('native decode failed')

    monkeypatch.setattr(preview.cv2, 'imdecode', failure)
    with pytest.raises(ValueError, match='Could not convert preview JPEG') as result:
        preview.preview_jpeg(jpeg('red'))
    assert isinstance(result.value.__cause__, preview.cv2.error)


def use_source(monkeypatch, source):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=source))
    monkeypatch.setattr(preview.httpx, 'AsyncClient', lambda **kw: original(transport=transport, **kw))


def test_stream_recovers_after_native_decoder_rejects_a_frame(monkeypatch):
    async def run():
        rejected = asyncio.Event()
        closed = []
        loop = asyncio.get_running_loop()
        convert = preview.preview_jpeg

        class Source(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield preview.multipart(corrupt_jpeg())
                # Ensure the bad image reaches the real decoder, rather than
                # merely being replaced in the latest-frame queue.
                await rejected.wait()
                yield preview.multipart(jpeg('blue', (320, 240)))

            async def aclose(self):
                closed.append(True)

        def observe_rejection(frame):
            try:
                return convert(frame)
            except ValueError:
                loop.call_soon_threadsafe(rejected.set)
                raise

        use_source(monkeypatch, Source())
        monkeypatch.setattr(preview, 'preview_jpeg', observe_rejection)
        stream = preview.preview_stream('http://test/stream')
        frame = preview.MjpegFrames().feed(await asyncio.wait_for(anext(stream), 2))
        with Image.open(BytesIO(frame)) as image:
            red, green, blue = image.getpixel((10, 10))
            assert blue > 240 and red < 10 and green < 10
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert rejected.is_set() and closed

    asyncio.run(run())


@pytest.mark.parametrize('fps', [24, 30])
def test_preview_caps_conversion_starts_without_catchup(monkeypatch, fps):
    monkeypatch.setattr(preview, 'PREVIEW_FPS', fps)
    async def run():
        incoming = asyncio.Queue()
        closed = []
        now = [0.0]
        starts = []

        class Source(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    yield preview.multipart(await incoming.get())

            async def aclose(self):
                closed.append(True)

        async def sleep(delay):
            now[0] += delay
            await asyncio.sleep(0)

        def convert(frame):
            starts.append(now[0])
            return frame

        use_source(monkeypatch, Source())
        monkeypatch.setattr(preview, 'preview_jpeg', convert)
        stream = preview.preview_stream('http://test/stream', clock=lambda: now[0], sleep=sleep)
        try:
            for index in range(49):
                incoming.put_nowait(str(index).encode())
                assert preview.MjpegFrames().feed(await anext(stream)) == str(index).encode()
            assert starts == pytest.approx([index / fps for index in range(49)])
            # A slow UI resumes with a current frame, without trying to catch
            # up missed display slots or doing an unnecessary pacing sleep.
            now[0] = 10.0
            incoming.put_nowait(b'after-pause')
            assert preview.MjpegFrames().feed(await anext(stream)) == b'after-pause'
            assert starts[-1] == 10.0
        finally:
            await stream.aclose()
        assert closed

    asyncio.run(run())


def test_frames_arriving_during_pacing_replace_the_waiting_frame(monkeypatch):
    async def run():
        incoming = asyncio.Queue()
        received = asyncio.Queue()
        pacing = asyncio.Event()
        release = asyncio.Event()
        converted = []
        closed = []
        now = [0.0]

        class Source(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    frame = await incoming.get()
                    yield preview.multipart(frame)
                    received.put_nowait(frame)

            async def aclose(self):
                closed.append(True)

        async def sleep(delay):
            pacing.set()
            await release.wait()
            now[0] += delay

        def convert(frame):
            converted.append(frame)
            return frame

        use_source(monkeypatch, Source())
        monkeypatch.setattr(preview, 'preview_jpeg', convert)
        stream = preview.preview_stream('http://test/stream', clock=lambda: now[0], sleep=sleep)
        try:
            incoming.put_nowait(b'first')
            assert preview.MjpegFrames().feed(await anext(stream)) == b'first'
            assert await received.get() == b'first'
            incoming.put_nowait(b'old')
            pending = asyncio.create_task(anext(stream))
            await asyncio.wait_for(pacing.wait(), 2)
            assert await received.get() == b'old'
            for value in (b'newer', b'newest'):
                incoming.put_nowait(value)
                assert await received.get() == value
            release.set()
            assert preview.MjpegFrames().feed(await pending) == b'newest'
            assert converted == [b'first', b'newest']
        finally:
            release.set()
            await stream.aclose()
        assert closed

    asyncio.run(run())


def test_slow_conversion_does_not_block_receiver_or_queue_old_frames(monkeypatch):
    async def run():
        incoming = asyncio.Queue()
        received = asyncio.Queue()
        entered = asyncio.Event()
        release = threading.Event()
        converted = []
        closed = []
        loop = asyncio.get_running_loop()

        class Source(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    frame = await incoming.get()
                    yield preview.multipart(frame)
                    received.put_nowait(frame)

            async def aclose(self):
                closed.append(True)

        def convert(frame):
            converted.append(frame)
            if len(converted) == 1:
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(timeout=3)
            return frame

        use_source(monkeypatch, Source())
        monkeypatch.setattr(preview, 'preview_jpeg', convert)
        stream = preview.preview_stream('http://test/stream')
        incoming.put_nowait(b'first')
        pending = asyncio.create_task(anext(stream))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert await received.get() == b'first'
            for value in (b'old', b'newest'):
                incoming.put_nowait(value)
                assert await asyncio.wait_for(received.get(), 2) == value
            release.set()
            assert preview.MjpegFrames().feed(await pending) == b'first'
            assert preview.MjpegFrames().feed(await anext(stream)) == b'newest'
            assert converted == [b'first', b'newest']
        finally:
            release.set()
            if not pending.done():
                await pending
            await stream.aclose()
        assert closed

    asyncio.run(run())


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('failure', [False, True])
def test_last_frame_survives_eof_and_upstream_errors_are_reported(monkeypatch, failure, native):
    async def run():
        closed = []

        class Source(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield preview.multipart(b'last-frame')
                if failure:
                    raise httpx.ReadError('camera disconnected')

            async def aclose(self):
                closed.append(True)

        use_source(monkeypatch, Source())
        monkeypatch.setattr(preview, 'preview_jpeg', lambda frame: frame)
        stream = preview.preview_stream('http://test/stream', native=native)
        assert preview.MjpegFrames().feed(await anext(stream)) == b'last-frame'
        with pytest.raises(httpx.ReadError if failure else StopAsyncIteration):
            await anext(stream)
        assert closed

    asyncio.run(run())


def test_cancelling_during_pacing_closes_upstream(monkeypatch):
    async def run():
        incoming = asyncio.Queue()
        pacing = asyncio.Event()
        closed = []

        class Source(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    yield preview.multipart(await incoming.get())

            async def aclose(self):
                closed.append(True)

        async def sleep(delay):
            pacing.set()
            await asyncio.Event().wait()

        use_source(monkeypatch, Source())
        monkeypatch.setattr(preview, 'preview_jpeg', lambda frame: frame)
        stream = preview.preview_stream('http://test/stream', clock=lambda: 0.0, sleep=sleep)
        incoming.put_nowait(b'first')
        assert await anext(stream)
        incoming.put_nowait(b'second')
        pending = asyncio.create_task(anext(stream))
        await asyncio.wait_for(pacing.wait(), 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert closed

    asyncio.run(run())


@pytest.mark.parametrize('native', [False, True])
def test_closing_slow_viewer_with_pending_frame_does_not_wait_for_queue(monkeypatch, native):
    async def run():
        incoming = asyncio.Queue()
        received = asyncio.Queue()
        closed = []

        class Source(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    frame = await incoming.get()
                    yield preview.multipart(frame)
                    received.put_nowait(frame)

            async def aclose(self):
                closed.append(True)

        use_source(monkeypatch, Source())
        monkeypatch.setattr(preview, 'preview_jpeg', lambda frame: frame)
        stream = preview.preview_stream('http://test/stream', native=native)
        incoming.put_nowait(b'first')
        assert await anext(stream)
        assert await received.get() == b'first'
        incoming.put_nowait(b'pending')
        assert await received.get() == b'pending'
        await asyncio.wait_for(stream.aclose(), 2)
        assert closed

    asyncio.run(run())


@pytest.mark.parametrize('native', [False, True])
def test_slow_display_keeps_latest_frame_and_cancellation_closes_upstream(monkeypatch, native):
    original=httpx.AsyncClient;closed=[];sent=[]
    async def run():
        release=asyncio.Event();ready=asyncio.Event()
        class Source(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield preview.multipart(jpeg('red'))
                await release.wait()
                for color in ('green','blue','yellow'):
                    sent.append(color);yield preview.multipart(jpeg(color));await asyncio.sleep(0)
                ready.set()
                await asyncio.Event().wait()
            async def aclose(self):closed.append(True)
        monkeypatch.setattr(preview.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(lambda _:httpx.Response(200,stream=Source())),**kw))
        stream=preview.preview_stream('http://test/stream', native=native)
        assert await anext(stream)
        release.set();await ready.wait()
        frame=preview.MjpegFrames().feed(await anext(stream))
        color=Image.open(BytesIO(frame)).getpixel((10,10))
        assert color[0]>200 and color[1]>200 and color[2]<30,color
        await stream.aclose()
    asyncio.run(run());assert closed and len(sent)==3


def test_native_desktop_preserves_every_jpeg_without_conversion_or_second_clock(monkeypatch):
    async def run():
        incoming = asyncio.Queue()
        closed = []

        class Source(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    yield preview.multipart(await incoming.get())

            async def aclose(self):
                closed.append(True)

        def forbidden(*args):
            pytest.fail('Native desktop frames must not be decoded/re-encoded or paced twice')

        use_source(monkeypatch, Source())
        monkeypatch.setattr(preview, 'preview_jpeg', forbidden)
        now = [0.0]
        stream = preview.preview_stream('http://test/stream', native=True,
                                        clock=lambda: now[0], sleep=forbidden)
        originals = [jpeg('red', (1920, 1080)), jpeg('blue', (1280, 1024))]
        try:
            # Early/late 30 Hz source frames must not acquire a second pacing
            # delay or a second compression loss. Slow viewers are covered above.
            for index in range(90):
                now[0] += (1 / 30) + (0.004 if index % 2 else -0.004)
                original = originals[index % 2]
                incoming.put_nowait(original)
                displayed = preview.MjpegFrames().feed(await asyncio.wait_for(anext(stream), 2))
                assert displayed == original
        finally:
            await stream.aclose()
        assert closed == [True]

    asyncio.run(run())


def test_stalled_native_camera_does_not_block_other_two_streams(monkeypatch):
    async def run():
        queues = {role: asyncio.Queue() for role in ('left', 'center', 'right')}
        closed = []
        original = httpx.AsyncClient

        class Source(httpx.AsyncByteStream):
            def __init__(self, role):
                self.role = role

            async def __aiter__(self):
                while True:
                    yield preview.multipart(await queues[self.role].get())

            async def aclose(self):
                closed.append(self.role)

        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, stream=Source(request.url.path.strip('/'))))
        monkeypatch.setattr(preview.httpx, 'AsyncClient',
                            lambda **kw: original(transport=transport, **kw))
        streams = {role: preview.preview_stream('http://test/' + role, native=True)
                   for role in queues}
        stalled = asyncio.create_task(anext(streams['center']))
        try:
            # Center delivers no bytes at all. Both wrists must still advance
            # independently, not wait on a shared frame barrier/encoder.
            for index in range(60):
                for role in ('left', 'right'):
                    data = f'{role}-{index}'.encode()
                    queues[role].put_nowait(data)
                    frame = await asyncio.wait_for(anext(streams[role]), 1)
                    assert preview.MjpegFrames().feed(frame) == data
            assert not stalled.done()
            queues['center'].put_nowait(b'recovered')
            assert preview.MjpegFrames().feed(await asyncio.wait_for(stalled, 1)) == b'recovered'
        finally:
            if not stalled.done():
                stalled.cancel()
                await asyncio.gather(stalled, return_exceptions=True)
            for stream in streams.values():
                await stream.aclose()
        assert sorted(closed) == ['center', 'left', 'right']

    asyncio.run(run())
