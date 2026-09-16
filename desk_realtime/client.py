"""WebSocket client: heartbeat + exponential backoff reconnect → DeskBus."""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any

# Prefer vendored websockets from grok-trading-desk when system pip lacks it
_VENDOR = Path(__file__).resolve().parents[1] / "grok-trading-desk" / ".vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

from desk_realtime.bus import DeskBus
from desk_realtime.schema import normalize_ws_event

DEFAULT_URL = os.environ.get("DESK_WS_URL", "ws://127.0.0.1:8765")
HEARTBEAT_SEC = float(os.environ.get("DESK_WS_HEARTBEAT", "8"))
ENABLED = os.environ.get("DESK_WS_ENABLED", "1").strip() not in ("0", "false", "False")

_started = False
_start_lock = threading.Lock()


def ensure_ws_client(url: str | None = None) -> None:
    """Idempotent: start one background WS client for this Streamlit process."""
    global _started
    if not ENABLED:
        DeskBus.set_status("disconnected", "DESK_WS_ENABLED=0")
        return
    with _start_lock:
        if _started:
            return
        _started = True
        target = url or DEFAULT_URL
        t = threading.Thread(
            target=_thread_main,
            args=(target,),
            name="desk-ws-client",
            daemon=True,
        )
        t.start()


def _thread_main(url: str) -> None:
    try:
        asyncio.run(_client_loop(url))
    except Exception as exc:  # noqa: BLE001
        from desk_realtime.secrets import sanitize_exc
        DeskBus.set_status("disconnected", sanitize_exc(exc))


async def _client_loop(url: str) -> None:
    try:
        import websockets
        from websockets.exceptions import ConnectionClosed
    except ImportError as exc:
        DeskBus.set_status("disconnected", f"websockets missing: {exc}")
        return

    backoff = 1.0
    while True:
        DeskBus.set_status("connecting")
        try:
            async with websockets.connect(
                url,
                ping_interval=HEARTBEAT_SEC,
                ping_timeout=HEARTBEAT_SEC * 2,
                close_timeout=2,
                max_size=2_000_000,
            ) as ws:
                DeskBus.set_status("connected")
                backoff = 1.0
                await asyncio.gather(
                    _recv_loop(ws),
                    _app_heartbeat(ws),
                )
        except asyncio.CancelledError:
            raise
        except ConnectionClosed as exc:
            from desk_realtime.secrets import sanitize_exc
            DeskBus.set_status("disconnected", sanitize_exc(exc))
        except OSError as exc:
            from desk_realtime.secrets import sanitize_exc
            DeskBus.set_status("disconnected", sanitize_exc(exc))
        except Exception as exc:  # noqa: BLE001
            from desk_realtime.secrets import sanitize_exc
            DeskBus.set_status("disconnected", sanitize_exc(exc))

        await asyncio.sleep(backoff)
        # Exponential backoff + jitter — avoid hammering RPC / WS after disconnect
        jitter = random.uniform(0, backoff * 0.3)
        backoff = min(backoff * 2.0 + jitter, 60.0)


async def _recv_loop(ws: Any) -> None:
    async for raw in ws:
        if raw is None:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = str(raw).strip()
        if not text:
            continue
        if text in ("ping", "pong"):
            if text == "ping":
                await ws.send("pong")
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("op") == "ping":
            await ws.send(json.dumps({"op": "pong", "t": time.time()}))
            continue
        if isinstance(payload, list):
            for item in payload:
                _ingest_one(item)
        else:
            _ingest_one(payload)


async def _app_heartbeat(ws: Any) -> None:
    """Application-level heartbeat in addition to protocol ping."""
    while True:
        await asyncio.sleep(HEARTBEAT_SEC)
        try:
            await ws.send(json.dumps({"op": "ping", "t": time.time()}))
        except Exception:
            return


def _ingest_one(payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    op = str(payload.get("op") or "").lower()
    status = str(payload.get("status") or payload.get("type") or "").upper()
    # Telemetry → topbar KPI bind (no jsonl clutter)
    if op == "metrics" or status == "METRICS":
        DeskBus.set_metrics(payload)
        return
    if op == "halt" or status in ("HALT", "STOP_ALL"):
        DeskBus.set_halted(True)
        DeskBus.set_metrics({"desk_mode": "HALTED"})
        row = normalize_ws_event({
            **payload,
            "status": "HOLD_OFF",
            "log_text": payload.get("log_text") or "EMERGENCY STOP · scanning paused",
            "agent_type": "EXIT",
        })
        if row:
            DeskBus.push(row)
        return
    if op == "resume" or status == "RESUME":
        DeskBus.set_halted(False)
        DeskBus.set_metrics({"desk_mode": "LIVE"})
        return
    row = normalize_ws_event(payload)
    if row:
        DeskBus.push(row)


def inject_local(packets: list[dict[str, Any]]) -> int:
    """Dev/mock path: push packets without a live server (same dispatch path)."""
    n = 0
    for p in packets:
        row = normalize_ws_event(p)
        if row:
            DeskBus.push(row)
            n += 1
    return n
