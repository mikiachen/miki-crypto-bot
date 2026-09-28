"""Robinhood Chain on-chain intel (MadeOnSol RHC API). Read-only.

Surfaces KOL hot tokens (BASIC) + optional smart-money / Uni tape (PRO+).
Never buys or signs. RH Uniswap local-sign remains the only fill path.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from desk_realtime.secrets import redact_text, sanitize_exc

log = logging.getLogger("rhc_intel")

_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = _ROOT / "grok-trading-desk" / "logs"
STATE_PATH = LOG_DIR / "rhc_intel.json"
JSONL_PATH = LOG_DIR / "rhc_intel.jsonl"

_API = (os.environ.get("RHC_API_BASE") or "https://madeonsol.com/api/v1").rstrip("/")
_CACHE: dict[str, Any] = {"ts": 0.0, "payload": {}}


def enabled() -> bool:
    return os.environ.get("RHC_INTEL", "1").strip().lower() not in ("0", "false", "off", "no")


def api_key() -> str:
    return (
        os.environ.get("RHC_API_KEY")
        or os.environ.get("MADEONSOL_API_KEY")
        or os.environ.get("MSK_API_KEY")
        or ""
    ).strip()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(row: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    safe = {k: redact_text(v) if isinstance(v, str) else v for k, v in row.items()}
    with JSONL_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(safe, ensure_ascii=False) + "\n")


def read_intel() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return {"ok": False, "mode": "read_only", "hot": [], "smart_money": [], "trades": []}
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {"ok": False}
    except Exception:
        return {"ok": False, "hot": [], "smart_money": [], "trades": []}


def theme_line(limit: int = 4) -> str:
    st = read_intel()
    if not st.get("ok"):
        reason = str(st.get("reason") or "unread")
        return f"rhc · {reason}"
    bits: list[str] = []
    for row in (st.get("hot") or [])[:limit]:
        sym = str(row.get("symbol") or row.get("token") or "?")[:10]
        bits.append(sym)
    sm = st.get("smart_money") or []
    if sm:
        bits.append(f"sm×{len(sm)}")
    trades = st.get("trades") or []
    if trades:
        bits.append(f"tape×{len(trades)}")
    if not bits:
        return "rhc quiet · no hot / tape"
    return "rhc · " + " · ".join(bits)


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    key = api_key()
    if not key:
        raise RuntimeError("RHC_API_KEY unread")
    q = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{_API}{path}" + (f"?{q}" if q else "")
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "miki-rhc-intel/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode(errors="replace")[:200]
        except Exception:
            body = ""
        raise RuntimeError(f"HTTP {exc.code} · {body or exc.reason}") from None
    if not isinstance(data, dict):
        raise RuntimeError("rhc shape unexpected")
    return data


def _pick_list(data: dict[str, Any], *keys: str) -> list[Any]:
    for k in keys:
        raw = data.get(k)
        if isinstance(raw, list):
            return raw
    # Some endpoints return {data:[...]}
    nested = data.get("data")
    if isinstance(nested, dict):
        for k in keys:
            raw = nested.get(k)
            if isinstance(raw, list):
                return raw
        if isinstance(nested.get("items"), list):
            return nested["items"]
    if isinstance(nested, list):
        return nested
    return []


def fetch_hot_tokens(limit: int = 8) -> list[dict[str, Any]]:
    """BASIC: KOL-attributed hot tokens on RH Chain."""
    data = _get("/rhc/kol/hot-tokens", {"limit": limit})
    rows = _pick_list(data, "tokens", "hot_tokens", "items", "results")
    out: list[dict[str, Any]] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        addr = str(
            row.get("token_address")
            or row.get("address")
            or row.get("token")
            or row.get("mint")
            or ""
        ).lower()
        sym = str(
            row.get("token_symbol")
            or row.get("symbol")
            or row.get("ticker")
            or row.get("token_name")
            or row.get("name")
            or "?"
        )
        out.append({
            "symbol": sym[:16],
            "name": str(row.get("token_name") or row.get("name") or "")[:48],
            "address": addr,
            "kol_count": int(row.get("kols_buying") or row.get("kol_count") or row.get("kols") or row.get("mentions") or 0),
            "buys": int(row.get("buys") or 0),
            "sells": int(row.get("sells") or 0),
            "volume_eth": float(row.get("buy_eth") or row.get("volume_eth") or row.get("eth_volume") or 0),
            "net_eth": float(row.get("net_eth") or 0),
            "market_cap_usd": float(row.get("market_cap_usd") or 0),
            "launchpad": str(row.get("launchpad") or ""),
            "source": "kol_hot",
        })
    return out


def fetch_smart_money(limit: int = 8) -> list[dict[str, Any]]:
    """PRO+: alpha wallet ranking. Empty list if tier blocks."""
    data = _get(
        "/rhc/alpha-wallets",
        {
            "classification": "smart_money",
            "min_memecoin_share": float(os.environ.get("RHC_MIN_MEME_SHARE", "0.5")),
            "sort": "net_eth",
            "limit": limit,
        },
    )
    rows = _pick_list(data, "wallets", "items", "results")
    out: list[dict[str, Any]] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        wallet = str(row.get("wallet") or row.get("address") or "")
        out.append({
            "wallet": wallet[:12] + "…" if len(wallet) > 14 else wallet,
            "net_eth": float(row.get("net_eth") or 0),
            "win_rate": float(row.get("win_rate") or 0),
            "likely_bot": bool(row.get("likely_bot")),
            "source": "alpha_wallets",
        })
    return out


def fetch_trades(limit: int = 8) -> list[dict[str, Any]]:
    """PRO+: recent Uniswap v2/v3/v4 tape."""
    data = _get("/rhc/trades", {"limit": limit})
    rows = _pick_list(data, "trades", "items", "results")
    out: list[dict[str, Any]] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or row.get("token_symbol") or "?")
        out.append({
            "symbol": sym[:12],
            "side": str(row.get("side") or row.get("type") or ""),
            "eth_amount": float(row.get("eth_amount") or row.get("amount_eth") or 0),
            "trader": str(row.get("trader_eoa") or row.get("trader") or "")[:10],
            "source": "uni_tape",
        })
    return out


def refresh(*, force: bool = False) -> dict[str, Any]:
    """Poll RHC intel into state file. Never a buy signal."""
    if not enabled():
        payload = {
            "ok": False,
            "mode": "read_only",
            "executes": False,
            "reason": "RHC_INTEL off",
            "hot": [],
            "smart_money": [],
            "trades": [],
            "updated_at": time.time(),
        }
        _atomic_write(STATE_PATH, payload)
        return payload

    ttl = float(os.environ.get("RHC_INTEL_TTL_SEC", "45"))
    if not force and _CACHE.get("payload") and time.time() - float(_CACHE.get("ts") or 0) < ttl:
        return dict(_CACHE["payload"])

    if not api_key():
        payload = {
            "ok": False,
            "mode": "read_only",
            "executes": False,
            "reason": "RHC_API_KEY unread · set MADEONSOL/RHC key",
            "hot": [],
            "smart_money": [],
            "trades": [],
            "updated_at": time.time(),
        }
        _atomic_write(STATE_PATH, payload)
        _CACHE["ts"] = time.time()
        _CACHE["payload"] = payload
        return payload

    hot: list[dict[str, Any]] = []
    smart: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    notes: list[str] = []
    # Free/BASIC: skip PRO endpoints unless explicitly enabled.
    want_pro = os.environ.get("RHC_PRO", "0").strip().lower() in ("1", "true", "yes", "on")

    try:
        hot = fetch_hot_tokens(int(os.environ.get("RHC_HOT_LIMIT", "8")))
    except Exception as exc:  # noqa: BLE001
        notes.append(f"hot {sanitize_exc(exc)}")
        log.info("rhc hot: %s", sanitize_exc(exc))

    if want_pro:
        try:
            smart = fetch_smart_money(int(os.environ.get("RHC_SM_LIMIT", "6")))
        except Exception as exc:  # noqa: BLE001
            notes.append(f"sm {sanitize_exc(exc)}")
            log.info("rhc smart_money: %s", sanitize_exc(exc))

        try:
            trades = fetch_trades(int(os.environ.get("RHC_TAPE_LIMIT", "8")))
        except Exception as exc:  # noqa: BLE001
            notes.append(f"tape {sanitize_exc(exc)}")
            log.info("rhc trades: %s", sanitize_exc(exc))
    else:
        notes.append("BASIC · set RHC_PRO=1 for smart-money/tape")

    ok = bool(hot or smart or trades)
    payload = {
        "ok": ok,
        "mode": "read_only",
        "executes": False,
        "chain": "robinhood",
        "chain_id": 4663,
        "reason": "ok" if ok else (" · ".join(notes) if notes else "empty"),
        "notes": notes[:4],
        "hot": hot,
        "smart_money": smart,
        "trades": trades,
        "updated_at": time.time(),
        "note": "intel only · not a RH buy trigger",
    }
    _atomic_write(STATE_PATH, payload)
    _append_jsonl({"ts": time.time(), "hot_n": len(hot), "sm_n": len(smart), "tape_n": len(trades), "ok": ok})
    _CACHE["ts"] = time.time()
    _CACHE["payload"] = payload
    return payload
