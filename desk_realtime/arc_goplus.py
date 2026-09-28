"""GoPlus token security scan.

The marketing site https://gopluslabs.io is not the API. The read endpoint is
token_security on api.gopluslabs.io. Robinhood Chain id 4663 is supported.
Honeypot or cannot_sell stops a buy. Unsupported / empty / error also stops a buy
unless the caller treats provider_na as soft.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from desk_realtime.secrets import redact_text, sanitize_exc

API = "https://api.gopluslabs.io/api/v1/token_security"
TOKEN_API = "https://api.gopluslabs.io/api/v1/token"
SNAP = Path(__file__).resolve().parents[1] / "grok-trading-desk" / "logs" / "goplus_scan.json"
HONEYPOT_LINE = "[GOPLUS DETECTED HONEYPOT] 拦截到貔貅盘，已自动强行中断购买！"
_CACHE: dict[str, dict[str, Any]] = {}
_ACCESS: dict[str, Any] = {"token": "", "exp": 0.0}


def _app_creds() -> tuple[str, str]:
    """Console app key + secret. Falls back to GOPLUS_API_KEY / GOPLUS_API_SECRET names."""
    key = (
        os.environ.get("GOPLUS_APP_KEY")
        or os.environ.get("GOPLUS_API_KEY")
        or ""
    ).strip()
    secret = (
        os.environ.get("GOPLUS_APP_SECRET")
        or os.environ.get("GOPLUS_API_SECRET")
        or ""
    ).strip()
    return key, secret


def _access_token() -> str:
    """Exchange app key/secret for a short-lived Bearer token. Empty if unset."""
    import hashlib
    import urllib.parse
    import urllib.request

    key, secret = _app_creds()
    if not key or not secret:
        return ""
    now = time.time()
    if _ACCESS.get("token") and now < float(_ACCESS.get("exp") or 0) - 30:
        return str(_ACCESS.get("token") or "")
    ts = int(now)
    sign = hashlib.sha1(f"{key}{ts}{secret}".encode()).hexdigest()
    # Official docs accept JSON; some console apps only accept form bodies.
    payloads = [
        ("application/json", json.dumps({"app_key": key, "sign": sign, "time": ts}).encode()),
        (
            "application/x-www-form-urlencoded",
            urllib.parse.urlencode({"app_key": key, "sign": sign, "time": str(ts)}).encode(),
        ),
    ]
    last = "goplus token rejected"
    for ctype, body in payloads:
        req = urllib.request.Request(
            TOKEN_API,
            data=body,
            headers={
                "Content-Type": ctype,
                "Accept": "application/json",
                "User-Agent": "miki-desk-audit/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001
            last = sanitize_exc(exc)
            continue
        if data.get("code") not in (1, "1"):
            last = str(data.get("message") or data.get("code") or "goplus token rejected")
            continue
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        token = str(result.get("access_token") or "")
        expires = float(result.get("expires_in") or 7200)
        if not token:
            last = "goplus access_token empty"
            continue
        _ACCESS["token"] = token
        _ACCESS["exp"] = now + max(60.0, expires)
        return token
    raise RuntimeError(str(last)[:120])


def _auth_headers() -> dict[str, str]:
    headers = {"User-Agent": "miki-desk-audit/1.0", "Accept": "application/json"}
    try:
        token = _access_token()
    except Exception as exc:  # noqa: BLE001
        # Key/secret present but token failed — still try unauthenticated free tier.
        log_msg = sanitize_exc(exc)
        headers["X-GoPlus-Auth-Error"] = log_msg[:80]
        return headers
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _flag(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        raw = row.get(key)
        if raw is None or raw == "":
            continue
        return str(raw)
    return ""


def _chain_id() -> str:
    desk = (os.environ.get("DESK_CHAIN") or "").strip().lower()
    if desk in ("robinhood", "rh", "rhchain"):
        return (os.environ.get("RH_CHAIN_ID") or "4663").strip()
    if (os.environ.get("GOPLUS_CHAIN_ID") or "").strip():
        return os.environ.get("GOPLUS_CHAIN_ID", "").strip()
    return (os.environ.get("ARC_CHAIN_ID") or "5042").strip()


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


async def scan_token(address: str, *, symbol: str = "", chain_id: str | None = None) -> dict[str, Any]:
    """Return a buy verdict. ok=True only when GoPlus explicitly says not a honeypot."""
    addr = (address or "").strip()
    now = time.time()
    cid = (chain_id or _chain_id()).strip()
    cache_key = f"{cid}:{addr.lower()}"
    cached = _CACHE.get(cache_key)
    if cached and now - float(cached.get("ts") or 0) < 45:
        return cached

    out: dict[str, Any] = {
        "ok": False,
        "buy": False,
        "honeypot": False,
        "risk": 0,
        "line": "",
        "symbol": symbol,
        "address": addr,
        "chain_id": cid,
        "is_open_source": "",
        "holder_count": "",
        "buy_tax": "",
        "sell_tax": "",
        "ts": now,
        "source": "goplus",
        "provider_na": False,
    }
    if not (addr.startswith("0x") and len(addr) == 42):
        out["line"] = "GOPLUS · bad address · buy blocked"
        out["risk"] = 100
        _CACHE[cache_key] = out
        _write(out)
        return out

    url = f"{API}/{cid}?contract_addresses={addr}"
    headers = _auth_headers()
    try:
        from desk_realtime.async_http import http_json

        data = await http_json("GET", url, headers=headers, timeout=8.0)
    except Exception as exc:  # noqa: BLE001
        out["line"] = f"GOPLUS unreachable · buy blocked · {sanitize_exc(exc)[:80]}"
        out["risk"] = 100
        _CACHE[cache_key] = out
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
        _CACHE[cache_key] = out
        _write(out)
        return out

    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    row = result.get(addr) or result.get(addr.lower()) or {}
    if not isinstance(row, dict) or not row:
        out["line"] = "GOPLUS · empty result · buy blocked"
        out["risk"] = 100
        _CACHE[cache_key] = out
        _write(out)
        return out

    honey = _flag(row, "is_honeypot")
    cannot = _flag(row, "cannot_sell_all", "cannot_sell")
    out["is_open_source"] = _flag(row, "is_open_source")
    out["holder_count"] = _flag(row, "holder_count")
    out["buy_tax"] = _flag(row, "buy_tax")
    out["sell_tax"] = _flag(row, "sell_tax")
    out["honeypot"] = honey == "1" or cannot == "1"
    if out["honeypot"]:
        out["line"] = HONEYPOT_LINE
        out["risk"] = 100
        out["buy"] = False
    else:
        out["ok"] = True
        out["buy"] = True
        out["risk"] = 0
        tax = out["sell_tax"] or out["buy_tax"] or "n/a"
        out["line"] = f"GOPLUS · clear · tax {tax} · holders {out['holder_count'] or '—'}"
    _CACHE[cache_key] = out
    _write(out)
    return out
