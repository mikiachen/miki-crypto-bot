"""IPC / local cache between trading engine process and Streamlit UI.

Engine writes atomically to `engine_state.json`.
UI (and any watcher) reads without calling RPC or holding the event loop.

Panic / valve commands:
  desk.halt   — pause auto-buy / scanning (Valve Gate CLOSED)
  desk.panic  — JSON {symbol, token_address, ts, reason} → immediate market flatten
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from desk_realtime.secrets import scrub_mapping

_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = _ROOT / "grok-trading-desk" / "logs"
STATE_PATH = LOG_DIR / "engine_state.json"
HALT_PATH = LOG_DIR / "desk.halt"
PANIC_PATH = LOG_DIR / "desk.panic"
VALVE_PATH = LOG_DIR / "desk.valve"  # OPEN | CLOSED

_lock = threading.Lock()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    data = json.dumps(scrub_mapping(payload), ensure_ascii=False, indent=0)
    fd, tmp = tempfile.mkstemp(dir=str(LOG_DIR), prefix=".eng_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_engine_state(payload: dict[str, Any]) -> None:
    """Trading loop → UI cache (safe fields only)."""
    row = dict(payload)
    row["updated_at"] = time.time()
    with _lock:
        _atomic_write(STATE_PATH, row)


def read_engine_state() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return {}
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return scrub_mapping(raw) if isinstance(raw, dict) else {}
    except Exception:
        return {}


def set_valve(closed: bool) -> None:
    """Valve Gate: CLOSED pauses new entries; OPEN resumes."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    VALVE_PATH.write_text("CLOSED" if closed else "OPEN", encoding="utf-8")
    if closed:
        HALT_PATH.write_text("1", encoding="utf-8")
    elif HALT_PATH.exists():
        try:
            HALT_PATH.unlink()
        except OSError:
            pass


def valve_closed() -> bool:
    if HALT_PATH.exists():
        return True
    if VALVE_PATH.is_file():
        return VALVE_PATH.read_text(encoding="utf-8").strip().upper() == "CLOSED"
    return False


def request_panic(
    *,
    symbol: str = "",
    token_address: str = "",
    reason: str = "ui",
) -> None:
    """One flatten request. A second click within 8s does not enqueue another."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if PANIC_PATH.is_file():
        age = time.time() - PANIC_PATH.stat().st_mtime
        if age < 8:
            return
    set_valve(True)
    payload = {
        "ts": time.time(),
        "symbol": str(symbol or "").lstrip("$").upper(),
        "token_address": str(token_address or ""),
        "reason": str(reason or "ui")[:80],
    }
    _atomic_write(PANIC_PATH, payload)


def consume_panic() -> dict[str, Any] | None:
    """Engine: read-and-clear panic request (once)."""
    if not PANIC_PATH.is_file():
        return None
    try:
        raw = json.loads(PANIC_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    try:
        PANIC_PATH.unlink()
    except OSError:
        pass
    return raw if isinstance(raw, dict) else {}
