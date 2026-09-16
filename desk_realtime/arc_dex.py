"""Dexscreener Arc tape. https://dexscreener.com/arc is indexed.

`/token-pairs/v1/arc/{token}` returns a bare list. Bonding-curve addresses
are not indexed; resolve token() first. Missing pairs stay unknown — never
invent liquidity, and never treat a pair as a buy.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

from desk_realtime.foundry_bin import is_allowed_rpc, rpc_endpoints
from desk_realtime.secrets import sanitize_exc

log = logging.getLogger("arc_dex")

_ROOT = Path(__file__).resolve().parents[1]
_BOOK = _ROOT / "grok-trading-desk" / "logs" / "dex_arc.json"
_CHAIN = (os.environ.get("ARC_DEXSCREENER_CHAIN") or "arc").strip() or "arc"
_TOKEN_SELECTOR = "0xfc0c546a"
_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_TTL = float(os.environ.get("ARC_DEX_TTL", "20"))


def dex_enabled() -> bool:
    raw = os.environ.get("ARC_AUDIT_DEX", "").strip()
    if raw == "":
        return os.environ.get("ARC_NETWORK", "mainnet").strip().lower() == "mainnet"
    return raw.lower() in ("1", "true", "yes", "on")


def _get_json(url: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "miki-desk-dex/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode())


def _as_pairs(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("pairs") or data.get("data") or []
    else:
        rows = []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("chainId") or "").lower() not in (_CHAIN.lower(), "arc", "5042"):
            continue
        out.append(row)
    return out


def _rpc(method: str, params: list[Any]) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    timeout = float(os.environ.get("ARC_RPC_TIMEOUT", "2"))
    last: Exception | None = None
    for url in rpc_endpoints():
        if not is_allowed_rpc(url):
            continue
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json", "User-Agent": "miki-desk-dex/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            if data.get("error"):
                raise RuntimeError(str(data["error"]))
            return data.get("result")
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError(sanitize_exc(last) if last else "rpc failed")


def token_of(curve: str) -> str:
    """token() on a Warp curve. Empty string if this address is already a token."""
    try:
        result = _rpc("eth_call", [{"to": curve, "data": _TOKEN_SELECTOR}, "latest"])
    except Exception:
        return ""
    if not isinstance(result, str) or len(result) < 66:
        return ""
    token = "0x" + result[-40:]
    if int(token, 16) == 0:
        return ""
    return token


def pairs_for(address: str, *, force: bool = False) -> list[dict[str, Any]]:
    key = (address or "").strip().lower()
    if not (key.startswith("0x") and len(key) >= 42):
        return []
    now = time.time()
    hit = _CACHE.get(key)
    if hit and not force and now - hit[0] < _TTL:
        return hit[1]
    pairs: list[dict[str, Any]] = []
    fetched = False
    try:
        pairs = _as_pairs(_get_json(f"https://api.dexscreener.com/token-pairs/v1/{_CHAIN}/{key}"))
        fetched = True
    except Exception as exc:  # noqa: BLE001
        log.info("dexscreener %s: %s", key[:10], sanitize_exc(exc))
    if not pairs:
        token = token_of(key)
        if token and token.lower() != key:
            try:
                pairs = _as_pairs(
                    _get_json(f"https://api.dexscreener.com/token-pairs/v1/{_CHAIN}/{token}")
                )
                fetched = True
            except Exception as exc:  # noqa: BLE001
                log.info("dexscreener token %s: %s", token[:10], sanitize_exc(exc))
                fetched = False
    if fetched:
        _CACHE[key] = (now, pairs)
    elif hit:
        return hit[1]
    return pairs


def best_pair(address: str) -> dict[str, Any] | None:
    rows = pairs_for(address)
    if not rows:
        return None

    def _liq(row: dict[str, Any]) -> float:
        liq = row.get("liquidity") if isinstance(row.get("liquidity"), dict) else {}
        try:
            return float(liq.get("usd") or 0)
        except (TypeError, ValueError):
            return 0.0

    return max(rows, key=_liq)


def liquidity_snapshot(address: str) -> dict[str, Any]:
    """Depth from Dexscreener. Unindexed → 0 and veto. No invented books."""
    pair = best_pair(address)
    if not pair:
        return {
            "liquidity_usdc": 0.0,
            "source": "dexscreener",
            "indexed": False,
            "veto": True,
            "symbol": "",
            "price_usd": None,
            "url": "",
        }
    liq = pair.get("liquidity") if isinstance(pair.get("liquidity"), dict) else {}
    try:
        depth = float(liq.get("usd") or 0)
    except (TypeError, ValueError):
        depth = 0.0
    base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
    try:
        price = float(pair.get("priceUsd"))
    except (TypeError, ValueError):
        price = None
    pair_addr = str(pair.get("pairAddress") or "")
    return {
        "liquidity_usdc": round(depth, 2),
        "source": "dexscreener",
        "indexed": True,
        "veto": depth <= 0,
        "symbol": str(base.get("symbol") or ""),
        "price_usd": price,
        "dex": str(pair.get("dexId") or ""),
        "url": f"https://dexscreener.com/{_CHAIN}/{pair_addr}" if pair_addr else "https://dexscreener.com/arc",
        "market_cap": pair.get("marketCap") or pair.get("fdv"),
    }


def sync_watch(addresses: list[str] | None = None) -> dict[str, Any]:
    """Write a small Arc tape. Does not broadcast a trade."""
    watched = []
    for raw in addresses or []:
        a = (raw or "").strip()
        if a.startswith("0x") and len(a) >= 42 and a.lower() not in {x.lower() for x in watched}:
            watched.append(a)
    if not watched:
        env = os.environ.get("ARC_WARP_CAS", "")
        for part in env.replace(";", ",").split(","):
            a = part.strip()
            if a.startswith("0x") and len(a) >= 42:
                watched.append(a)
    rows = []
    for addr in watched:
        snap = liquidity_snapshot(addr)
        snap["address"] = addr
        rows.append(snap)
    tape = _usdc_tape()
    book = {"chain": _CHAIN, "ts": time.time(), "rows": rows, "tape": tape}
    try:
        _BOOK.parent.mkdir(parents=True, exist_ok=True)
        _BOOK.write_text(json.dumps(book, indent=2), encoding="utf-8")
    except OSError as exc:
        log.info("dex book write failed: %s", sanitize_exc(exc))
    return book


def _usdc_tape(limit: int = 5) -> list[dict[str, Any]]:
    """Top Arc pairs quoting native USDC. Read-only market tape."""
    usdc = (
        os.environ.get("ARC_USDC")
        or "0x3600000000000000000000000000000000000000"
    ).strip()
    try:
        rows = _as_pairs(_get_json(f"https://api.dexscreener.com/token-pairs/v1/{_CHAIN}/{usdc}"))
    except Exception as exc:  # noqa: BLE001
        log.info("dexscreener tape: %s", sanitize_exc(exc))
        return []

    def _liq(row: dict[str, Any]) -> float:
        liq = row.get("liquidity") if isinstance(row.get("liquidity"), dict) else {}
        try:
            return float(liq.get("usd") or 0)
        except (TypeError, ValueError):
            return 0.0

    out = []
    for row in sorted(rows, key=_liq, reverse=True)[:limit]:
        base = row.get("baseToken") if isinstance(row.get("baseToken"), dict) else {}
        out.append({
            "symbol": str(base.get("symbol") or ""),
            "address": str(base.get("address") or ""),
            "liquidity_usdc": round(_liq(row), 2),
            "dex": str(row.get("dexId") or ""),
        })
    return out
