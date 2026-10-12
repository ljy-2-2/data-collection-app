"""Lightweight live display stream, independent of recording and upload scans."""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator

import httpx

DISPLAY_HZ = 30


async def telemetry_events(monitor_url: str) -> AsyncIterator[str]:
    # Reuse the loopback connection. Never call UmiManager.status(): it reads
    # process state, logs and the entire upload directory on every invocation.
    async with httpx.AsyncClient(timeout=0.75, trust_env=False) as client:
        while True:
            started = time.monotonic()
            delay = 1.0 / DISPLAY_HZ
            try:
                response = await client.get(f"{monitor_url.rstrip('/')}/api/state")
                response.raise_for_status()
                state = response.json()
                payload = {
                    "monitor_connected": bool(state.get("ros_connected")),
                    "ts": state.get("ts"),
                    "vr": state.get("vr", {}),
                    "encoders": state.get("encoders", {}),
                }
                encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False)
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                encoded = '{"monitor_connected":false,"vr":{},"encoders":{}}'
                delay = 1.0
            yield f"data:{encoded}\n\n"
            # Compensate for request time without accumulating a frame backlog.
            await asyncio.sleep(max(0, delay - (time.monotonic() - started)))
