import asyncio
from io import BytesIO
from pathlib import Path
import sys

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


@pytest.mark.parametrize('size,expected',[((1920,1080),(960,540)),((1280,1024),(960,768)),((320,240),(320,240))])
def test_only_preview_is_scaled_original_bytes_are_unchanged(size,expected):
    source=jpeg('red',size);saved=bytes(source)
    output=preview.preview_jpeg(source)
    assert Image.open(BytesIO(output)).size==expected
    assert Image.open(BytesIO(source)).size==size and source==saved


def test_slow_display_keeps_latest_frame_and_cancellation_closes_upstream(monkeypatch):
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
        stream=preview.preview_stream('http://test/stream')
        assert await anext(stream)
        release.set();await ready.wait()
        frame=preview.MjpegFrames().feed(await anext(stream))
        color=Image.open(BytesIO(frame)).getpixel((10,10))
        assert color[0]>200 and color[1]>200 and color[2]<30,color
        await stream.aclose()
    asyncio.run(run());assert closed and len(sent)==3
