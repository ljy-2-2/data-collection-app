import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teleop_control/src"))

from teleop_control import umi_telemetry
from teleop_control.app import create_app
from teleop_control.main import MockController


def test_live_stream_uses_one_client_and_excludes_slow_state(monkeypatch):
    requests = []
    clients = []
    original_client = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "ros_connected": True, "ts": len(requests),
            "vr": {"hmd": {"seq": len(requests)}},
            "encoders": {"left": {"position": len(requests) / 100}},
            "logs": ["not a live field"], "episode_list": ["not a live field"],
        })

    def factory(**kwargs):
        client = original_client(transport=httpx.MockTransport(respond), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(umi_telemetry.httpx, "AsyncClient", factory)

    async def consume():
        stream = umi_telemetry.telemetry_events("http://monitor.test/")
        result = [json.loads((await anext(stream))[5:]) for _ in range(3)]
        await stream.aclose()
        return result

    values = asyncio.run(consume())
    assert len(clients) == 1 and clients[0].is_closed
    assert all(str(request.url) == "http://monitor.test/api/state" for request in requests)
    assert [value["vr"]["hmd"]["seq"] for value in values] == [1, 2, 3]
    assert all(set(value) == {"monitor_connected", "ts", "vr", "encoders"} for value in values)


def test_upstream_failure_marks_offline_then_recovers(monkeypatch):
    count = 0
    original_client = httpx.AsyncClient

    def respond(request):
        nonlocal count
        count += 1
        if count == 1:
            raise httpx.ReadTimeout("disconnected", request=request)
        return httpx.Response(200, json={"ros_connected": True, "vr": {}, "encoders": {}})

    monkeypatch.setattr(umi_telemetry.httpx, "AsyncClient", lambda **kw:
        original_client(transport=httpx.MockTransport(respond), **kw))

    async def consume():
        stream = umi_telemetry.telemetry_events("http://monitor.test")
        first = json.loads((await anext(stream))[5:])
        second = json.loads((await anext(stream))[5:])
        await stream.aclose()
        return first, second

    first, second = asyncio.run(consume())
    assert first == {"monitor_connected": False, "vr": {}, "encoders": {}}
    assert second["monitor_connected"] is True


def test_telemetry_endpoint_never_scans_status_or_upload_directories(monkeypatch):
    def forbidden():
        raise AssertionError("live telemetry must not call status()")

    urls = []

    async def events(url):
        urls.append(url)
        yield 'data:{"monitor_connected":true}\n\n'

    monkeypatch.setattr("teleop_control.app.telemetry_events", events)
    app = create_app(MockController(), cors_origins=[], umi=SimpleNamespace(
        monitor_url="http://127.0.0.1:8770", status=forbidden))
    endpoint = next(route.endpoint for route in app.routes if route.path == "/umi/telemetry")

    async def consume():
        response = await endpoint()
        assert response.media_type == "text/event-stream"
        assert response.headers["cache-control"] == "no-store"
        return [part async for part in response.body_iterator]

    assert asyncio.run(consume()) == ['data:{"monitor_connected":true}\n\n']
    assert urls == ["http://127.0.0.1:8770"]
