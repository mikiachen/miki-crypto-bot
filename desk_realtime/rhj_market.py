"""Robinhood Stock Token market layer — RHJ prices + tradingCapabilities.

Read-only helpers for liquid gates. Prefer RHJ mid for premium when available;
enforce halt / session tradability before any buy. Never executes trades.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from typing import Any

from desk_realtime.secrets import sanitize_exc

log = logging.getLogger("rhj_market")

_RHJ = "https://api.robinhood.com/rhj"
_ASSETS: tuple[float, dict[str, dict[str, Any]]] = (0.0, {})
_QUOTES: dict[str, tuple[float, dict[str, Any]]] = {}


def enabled() -> bool:
    return (os.environ.get("RH_RHJ_MARKET") or "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def assets_cache_sec() -> float:
    try:
        return max(60.0, float(os.environ.get("RH_RHJ_ASSETS_CACHE_SEC", "600")))
    except ValueError:
        return 600.0


def quote_cache_sec() -> float:
    try:
        return max(5.0, float(os.environ.get("RH_RHJ_QUOTE_CACHE_SEC", "30")))
    except ValueError:
        return 30.0


def _get_json(url: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "miki-rh-desk/1.0"},
    )
    with urllib.request.urlopen(req, timeout=14) as resp:
        return json.loads(resp.read().decode())


def _num(raw: Any) -> float:
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def refresh_assets(*, force: bool = False) -> dict[str, dict[str, Any]]:
    """Map SYMBOL -> asset row (addr, multiplier, capabilities, status)."""
    global _ASSETS
    ts, rows = _ASSETS
    if rows and not force and time.time() - ts < assets_cache_sec():
        return rows
    try:
        data = _get_json(f"{_RHJ}/assets")
    except Exception as exc:  # noqa: BLE001
        log.info("rhj assets: %s", sanitize_exc(exc))
        return rows
    out: dict[str, dict[str, Any]] = {}
    for it in data.get("assets") or []:
        if not isinstance(it, dict):
            continue
        sym = str(it.get("tokenSymbol") or "").upper()
        if not sym:
            continue
        dep = (it.get("deployments") or [{}])[0] if isinstance(it.get("deployments"), list) else {}
        addr = str((dep or {}).get("contractAddress") or "").lower()
        out[sym] = {
            "symbol": sym,
            "address": addr,
            "status": str(it.get("status") or ""),
            "multiplier": _num(it.get("currentMultiplier") or 1.0) or 1.0,
            "capabilities": it.get("tradingCapabilities") if isinstance(it.get("tradingCapabilities"), dict) else {},
        }
    if out:
        _ASSETS = (time.time(), out)
    return out or rows


def asset(symbol: str) -> dict[str, Any] | None:
    sym = (symbol or "").upper().strip()
    if not sym:
        return None
    return refresh_assets().get(sym)


def fetch_quote(symbol: str, *, force: bool = False) -> dict[str, Any]:
    """RHJ /prices/{symbol} — bid/ask mid, halt flag. Cached ~30s."""
    sym = (symbol or "").upper().strip()
    empty = {
        "ok": False,
        "symbol": sym,
        "bid": 0.0,
        "ask": 0.0,
        "mid": 0.0,
        "halt": False,
        "generated_at": "",
        "reason": "unread",
    }
    if not sym:
        return empty
    hit = _QUOTES.get(sym)
    if hit and not force and time.time() - hit[0] < quote_cache_sec():
        return dict(hit[1])
    try:
        data = _get_json(f"{_RHJ}/prices/{sym}")
    except Exception as exc:  # noqa: BLE001
        empty["reason"] = sanitize_exc(exc)
        log.info("rhj price %s: %s", sym, empty["reason"])
        return empty
    quotes = data.get("quotes") or []
    if not quotes or not isinstance(quotes[0], dict):
        empty["reason"] = "quote empty"
        return empty
    q = quotes[0]
    bid = _num(q.get("bid"))
    ask = _num(q.get("ask"))
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else (bid or ask)
    out = {
        "ok": mid > 0,
        "symbol": sym,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "halt": bool(q.get("isTradingHalt")),
        "generated_at": str(q.get("generatedAt") or ""),
        "daily_high": _num(q.get("dailyHigh")),
        "daily_low": _num(q.get("dailyLow")),
        "reason": "ok" if mid > 0 else "mid unread",
    }
    _QUOTES[sym] = (time.time(), dict(out))
    return out


def _session_bucket(session: str) -> str:
    """Map desk session → RHJ capability bucket."""
    sess = (session or "").strip().lower()
    if sess in ("power_hour", "regular", "market", "rth"):
        return "market"
    if sess in ("pre_break", "premarket", "extended"):
        return "extended"
    if sess in ("overnight", "closed", "off", "weekend", "thin"):
        return "overnight"
    return "extended"


def capability_status(symbol: str, *, session: str) -> str:
    """Return TRADING_STATUS_* string for the session bucket."""
    row = asset(symbol)
    if not row:
        return ""
    caps = row.get("capabilities") or {}
    bucket = _session_bucket(session)
    leg = caps.get(bucket) if isinstance(caps, dict) else None
    if not isinstance(leg, dict):
        return ""
    return str(leg.get("fractional") or leg.get("whole") or "")


def tradable(symbol: str, *, session: str) -> bool:
    st = capability_status(symbol, session=session)
    return "TRADABLE" in st.upper() if st else False


def gate(symbol: str, *, session: str, token_address: str = "") -> tuple[str, str]:
    """Return (audit_code, reason). Empty code means pass.

    audit codes: RHJ_ASSET, RHJ_ADDR, RHJ_STATUS, RHJ_HALT, RHJ_CAPS
    Soft-pass when RHJ is unreachable so Yahoo gates still run.
    """
    if not enabled():
        return "", ""
    sym = (symbol or "").upper().strip()
    assets = refresh_assets()
    if not assets:
        # API down / first miss — do not freeze entry path.
        return "", ""
    row = assets.get(sym)
    if not row:
        return "RHJ_ASSET", f"rhj asset unread · {sym}"
    status = str(row.get("status") or "")
    if status and "ACTIVE" not in status.upper():
        return "RHJ_STATUS", f"rhj status {status}"
    expect = str(row.get("address") or "").lower()
    got = (token_address or "").lower()
    if expect and got and expect != got:
        return "RHJ_ADDR", f"rhj addr mismatch · expect {expect[:10]}"
    q = fetch_quote(sym)
    if q.get("halt"):
        return "RHJ_HALT", f"rhj trading halt · {sym}"
    caps = row.get("capabilities") or {}
    if caps and not tradable(sym, session=session):
        cap = capability_status(sym, session=session) or "unread"
        return "RHJ_CAPS", f"rhj not tradable · {session}/{_session_bucket(session)} · {cap}"
    if not q.get("ok"):
        # Soft: allow Yahoo premium path if quote unread (don't hard-block).
        return "", ""
    return "", ""


def mid_price(symbol: str) -> float:
    q = fetch_quote(symbol)
    return float(q.get("mid") or 0) if q.get("ok") else 0.0
