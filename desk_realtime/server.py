"""WebSocket fan-out server for desk live events.

Run:
  PYTHONPATH=grok-trading-desk/.vendor:. \\
    python -m desk_realtime.server --host 127.0.0.1 --port 8765
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

_VENDOR = Path(__file__).resolve().parents[1] / "grok-trading-desk" / ".vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

import websockets
from websockets.server import WebSocketServerProtocol

CLIENTS: set[WebSocketServerProtocol] = set()
LOCK = asyncio.Lock()


async def _register(ws: WebSocketServerProtocol) -> None:
    async with LOCK:
        CLIENTS.add(ws)
    await ws.send(
        json.dumps(
            {
                "op": "hello",
                "log_text": "desk ws online",
                "t": time.time(),
            }
        )
    )


async def _unregister(ws: WebSocketServerProtocol) -> None:
    async with LOCK:
        CLIENTS.discard(ws)


async def broadcast(payload: dict[str, Any] | str) -> None:
    if isinstance(payload, dict):
        raw = json.dumps(payload, ensure_ascii=False)
    else:
        raw = str(payload)
    async with LOCK:
        peers = list(CLIENTS)
    dead: list[WebSocketServerProtocol] = []
    for peer in peers:
        try:
            await peer.send(raw)
        except Exception:
            dead.append(peer)
    if dead:
        async with LOCK:
            for d in dead:
                CLIENTS.discard(d)


async def handler(ws: WebSocketServerProtocol) -> None:
    await _register(ws)
    try:
        async for raw in ws:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            text = str(raw).strip()
            if not text:
                continue
            if text == "ping":
                await ws.send("pong")
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and msg.get("op") == "ping":
                await ws.send(json.dumps({"op": "pong", "t": time.time()}))
                continue
            # Publisher clients: any JSON object is fan-out to all UIs
            if isinstance(msg, dict) and msg.get("op") != "pong":
                await broadcast(msg)
            elif isinstance(msg, list):
                for item in msg:
                    if isinstance(item, dict):
                        await broadcast(item)
    finally:
        await _unregister(ws)


async def main_async(host: str, port: int) -> None:
    async with websockets.serve(handler, host, port, ping_interval=8, ping_timeout=16):
        print(f"[desk-ws] listening ws://{host}:{port}", flush=True)
        await asyncio.Future()


def main() -> None:
    p = argparse.ArgumentParser(description="Desk realtime WebSocket server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()
    asyncio.run(main_async(args.host, args.port))


if __name__ == "__main__":
    main()
