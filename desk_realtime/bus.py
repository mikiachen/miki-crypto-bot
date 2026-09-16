"""Thread-safe event bus + connection status for Streamlit fragment drain."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any


class DeskBus:
    """Process-wide queue: WS client thread → Streamlit fragment (same-tick dispatch)."""

    _q: deque[dict[str, Any]] = deque(maxlen=2000)
    _lock = threading.Lock()
    _status = "boot"  # boot | connecting | connected | disconnected
    _last_event_at = 0.0
    _last_error = ""
    _seq = 0
    # Live KPI snapshot from backend METRICS packets (strict field bind)
    _metrics: dict[str, Any] = {}
    _halted = False

    @classmethod
    def status(cls) -> str:
        with cls._lock:
            return cls._status

    @classmethod
    def last_error(cls) -> str:
        with cls._lock:
            return cls._last_error

    @classmethod
    def set_status(cls, status: str, error: str = "") -> None:
        with cls._lock:
            cls._status = status
            if error:
                cls._last_error = error

    @classmethod
    def set_metrics(cls, payload: dict[str, Any]) -> None:
        """Merge server telemetry into the topbar KPI bind layer."""
        if not isinstance(payload, dict):
            return
        skip = {"op", "status", "type", "timestamp", "ts", "log_text", "kind"}
        with cls._lock:
            for k, v in payload.items():
                if k in skip or v is None:
                    continue
                cls._metrics[str(k)] = v
            cls._last_event_at = time.time()

    @classmethod
    def metrics(cls) -> dict[str, Any]:
        with cls._lock:
            return dict(cls._metrics)

    @classmethod
    def set_halted(cls, halted: bool) -> None:
        with cls._lock:
            cls._halted = bool(halted)

    @classmethod
    def halted(cls) -> bool:
        with cls._lock:
            return cls._halted

    @classmethod
    def push(cls, row: dict[str, Any]) -> None:
        with cls._lock:
            cls._seq += 1
            row = dict(row)
            row.setdefault("_bus_seq", cls._seq)
            cls._q.append(row)
            cls._last_event_at = time.time()

    @classmethod
    def drain(cls, limit: int = 200) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with cls._lock:
            while cls._q and len(out) < limit:
                out.append(cls._q.popleft())
        return out

    @classmethod
    def snapshot(cls) -> dict[str, Any]:
        with cls._lock:
            return {
                "status": cls._status,
                "queued": len(cls._q),
                "last_event_at": cls._last_event_at,
                "last_error": cls._last_error,
                "seq": cls._seq,
                "metrics": dict(cls._metrics),
                "halted": cls._halted,
            }
