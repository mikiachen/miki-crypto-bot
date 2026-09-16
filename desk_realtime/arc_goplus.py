"""GoPlus token security scan. Fail closed on Arc until the chain is supported.

The marketing site https://gopluslabs.io is not the API. The read endpoint is
token_security on api.gopluslabs.io. A key does not add chain 5042.
Honeypot or cannot_sell stops a buy. Unsupported / empty / error also stops a buy.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from desk_realtime.secrets import redact_text, sanitize_exc

API = "https://api.gopluslabs.io/api/v1/token_security"
SNAP = Path(__file__).resolve().parents[1] / "grok-trading-desk" / "logs" / "goplus_scan.json"
HONEYPOT_LINE = "[GOPLUS DETECTED HONEYPOT] 拦截到貔貅盘，已自动强行中断购买！"
_CACHE: dict[str, dict[str, Any]] = {}


def _flag(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        raw = row.get(key)
        if raw is None or raw == "":
            continue
        return str(raw)
    return ""


def _write(row: dict[str, Any]) -> None:
    try:
        SNAP.parent.mkdir(parents=True, exist_ok=True)
        SNAP.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def read_scan() -> dict[str, Any]:
    try:
        if SNAP.is_file():
            raw = json.loads(SNAP.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


async def scan_token(address: str, *, symbol: str = "") -> dict[str, Any]:
    """Return a buy verdict. ok=True only when GoPlus explicitly says not a honeypot."""
    addr = (address or "").strip()
    now = time.time()
    cached = _CACHE.get(addr.lower())
    if cached and now - float(cached.get("ts") or 0) < 45:
        return cached

    chain_id = (os.environ.get("ARC_CHAIN_ID") or "5042").strip()
    out: dict[str, Any] = {
        "ok": False,
        "buy": False,
        "honeypot": False,
        "risk": 0,
        "line": "",
        "symbol": symbol,
        "address": addr,
        "chain_id": chain_id,
        "is_open_source": "",
        "holder_count": "",
        "ts": now,
        "source": "goplus",
        "provider_na": False,
    }
    if not (addr.startswith("0x") and len(addr) == 42):
        out["line"] = "GOPLUS · bad address · buy blocked"
        out["risk"] = 100
        _CACHE[addr.lower()] = out
        _write(out)
        return out

    url = f"{API}/{chain_id}?contract_addresses={addr}"
    headers = {"User-Agent": "miki-desk-audit/1.0", "Accept": "application/json"}
    key = (os.environ.get("GOPLUS_API_KEY") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        from desk_realtime.async_http import http_json

        data = await http_json("GET", url, headers=headers, timeout=8.0)
    except Exception as exc:  # noqa: BLE001
        out["line"] = f"GOPLUS unreachable · buy blocked · {sanitize_exc(exc)[:80]}"
        out["risk"] = 100
        _CACHE[addr.lower()] = out
        _write(out)
        return out

    code = data.get("code")
    if code not in (1, "1"):
        msg = redact_text(str(data.get("message") or "rejected"))[:80]
        out["line"] = f"GOPLUS · chain unsupported or rejected · {msg} · provider n/a"
        out["risk"] = 0
        out["code"] = code
        out["provider_na"] = True
        out["honeypot"] = False
        _CACHE[addr.lower()] = out
        _write(out)
        return out

    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    row = result.get(addr) or result.get(addr.lower()) or {}
    if not isinstance(row, dict) or not row:
        out["line"] = "GOPLUS · empty result · buy blocked"
        out["risk"] = 100
        _CACHE[addr.lower()] = out
        _write(out)
        return out

    honey = _flag(row, "is_honeypot")
    cannot = _flag(row, "cannot_sell_all", "cannot_sell")
    out["is_open_source"] = _flag(row, "is_open_source")
    out["holder_count"] = _flag(row, "holder_count")
    out["honeypot"] = honey == "1" or cannot == "1"
    if out["honeypot"]:
        out["line"] = HONEYPOT_LINE
        out["risk"] = 100
        out["buy"] = False
    else:
        out["ok"] = True
        out["buy"] = True
        out["risk"] = 0
        out["line"] = "GOPLUS · not a honeypot"
    _CACHE[addr.lower()] = out
    _write(out)
    return out
