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
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from desk_realtime.foundry_bin import is_allowed_rpc, rpc_endpoints
from desk_realtime.secrets import sanitize_exc

log = logging.getLogger("arc_dex")

_ROOT = Path(__file__).resolve().parents[1]
_BOOK = _ROOT / "grok-trading-desk" / "logs" / "dex_arc.json"
_CHAIN = (os.environ.get("ARC_DEXSCREENER_CHAIN") or "arc").strip() or "arc"
_TOKEN_SELECTOR = "0xfc0c546a"
_SYMBOL_SELECTOR = "0x95d89b41"
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


def _decode_symbol(raw: str) -> str:
    h = raw[2:] if raw.startswith("0x") else raw
    if len(h) < 64 or len(h) % 2:
        return ""
    text = ""
    if len(h) == 64:
        try:
            text = bytes.fromhex(h).rstrip(b"\x00").decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            return ""
    else:
        try:
            strlen = int(h[64:128], 16)
        except ValueError:
            return ""
        if not 0 < strlen <= 32:
            return ""
        try:
            text = bytes.fromhex(h[128:128 + strlen * 2]).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            return ""
    out = "".join(ch for ch in text if ch.isalnum() or ch == "_")[:16]
    return out.upper()


def token_symbol(token: str) -> str:
    """ERC-20 symbol() via eth_call. Empty if the node does not answer."""
    addr = (token or "").strip()
    if not (addr.startswith("0x") and len(addr) == 42):
        return ""
    try:
        raw = _rpc("eth_call", [{"to": addr, "data": _SYMBOL_SELECTOR}, "latest"])
    except Exception:
        return ""
    if not isinstance(raw, str):
        return ""
    return _decode_symbol(raw)


_IDENT_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_IDENT_TTL = 60.0


def market_identity(curve: str) -> dict[str, str]:
    """Real ticker and token CA. Never invents a name when both reads miss."""
    curve = (curve or "").strip()
    key = curve.lower()
    now = time.time()
    hit = _IDENT_CACHE.get(key)
    if hit and now - hit[0] < _IDENT_TTL:
        return hit[1]
    token = token_of(curve) if curve.startswith("0x") else ""
    symbol = token_symbol(token) if token else ""
    source = "symbol()" if symbol else ""
    if not symbol:
        pair = best_pair(curve)
        base = pair.get("baseToken") if isinstance(pair, dict) else None
        if isinstance(base, dict):
            symbol = "".join(
                ch for ch in str(base.get("symbol") or "") if ch.isalnum() or ch == "_"
            )[:16].upper()
            if symbol:
                source = "dexscreener"
            listed = str(base.get("address") or "")
            if listed.startswith("0x") and len(listed) == 42 and not token:
                token = listed
    out = {
        "symbol": symbol,
        "token": token,
        "curve": curve,
        "source": source,
    }
    if key.startswith("0x"):
        _IDENT_CACHE[key] = (now, out)
    return out


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


def pair_links(address: str) -> dict[str, str]:
    """Dexscreener-listed website and X URL. Empty when the pair lists none."""
    pair = best_pair(address) or {}
    info = pair.get("info") if isinstance(pair.get("info"), dict) else {}
    website = ""
    sites = info.get("websites") if isinstance(info.get("websites"), list) else []
    if sites and isinstance(sites[0], dict):
        website = str(sites[0].get("url") or "")
    twitter = ""
    socials = info.get("socials") if isinstance(info.get("socials"), list) else []
    for row in socials:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("type") or "").lower()
        url = str(row.get("url") or "")
        if kind in ("twitter", "x") or "twitter.com/" in url or "x.com/" in url:
            twitter = url
            break
    return {"website": website, "twitter": twitter}


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
    board = ranked_board()
    tape = board[:8] if board else _usdc_tape()
    timelines = refresh_timelines()
    book = {
        "chain": _CHAIN,
        "ts": time.time(),
        "rows": rows,
        "tape": tape,
        "board_n": len(board),
        "timelines": {
            d: [
                {
                    "symbol": r.get("symbol") or "",
                    "chg": round(_num(r.get(_CHG_KEY[d])), 1),
                    "address": r.get("address") or "",
                }
                for r in rows_d[:3]
            ]
            for d, rows_d in timelines.items()
        },
        "timeline": timeline_line(timelines),
    }
    try:
        _BOOK.parent.mkdir(parents=True, exist_ok=True)
        _BOOK.write_text(json.dumps(book, indent=2), encoding="utf-8")
    except OSError as exc:
        log.info("dex book write failed: %s", sanitize_exc(exc))
    return book


_BOARD: tuple[float, list[dict[str, Any]]] = (0.0, [])
_BOARD_CURSOR = 0
_SKIP_SYMS = {"USDC", "EURC", "WXRP"}
# Public mainnet announcement. Until this is 24h old, h24 % is a first-print fiction.
_ARC_PUBLIC = datetime(2026, 9, 16, 10, 32, tzinfo=timezone.utc)
_NEW_POOLS: tuple[float, list[dict[str, Any]]] = (0.0, [])
_TREND_WINDOW: tuple[float, list[dict[str, Any]]] = (0.0, [])
# Dexscreener chips: 5M / 1H / 6H / 24H. Watch all of them. None of them is a buy.
_DURATIONS = ("5m", "1h", "6h", "24h")
_TIMELINE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_TIMELINE_CURSOR = 0
_CHG_KEY = {"5m": "chg_m5", "1h": "chg_h1", "6h": "chg_h6", "24h": "chg_h24"}


def _board_limit() -> int:
    try:
        return max(1, min(100, int(os.environ.get("ARC_DEX_TOP", "100"))))
    except ValueError:
        return 100


def _dex_label(dex_id: str) -> str:
    d = (dex_id or "").lower()
    if "uniswap" in d:
        return "uniswap"
    if "dyor" in d:
        return "dyorswap"
    if "sushi" in d:
        return "sushi"
    if "aero" in d:
        return "aerodrome"
    if "synth" in d:
        return "synthra"
    if "drop" in d:
        return "dropswap"
    return d.replace("-arc", "") or "dex"


def _symbol_from_pool_name(name: str) -> str:
    left = (name or "").split("/")[0].strip()
    out = "".join(ch for ch in left if ch.isalnum() or ch == "_")[:16]
    return out.upper()


def _gecko_ranked(limit: int) -> list[dict[str, Any]]:
    """Arc pools by 24h volume. Same venue as dexscreener.com/arc; public API caps at 30."""
    seen: dict[str, dict[str, Any]] = {}
    headers = {"Accept": "application/json", "User-Agent": "miki-desk-dex/1.0"}
    usdc = (
        os.environ.get("ARC_USDC") or "0x3600000000000000000000000000000000000000"
    ).lower()
    # Pools repeat across pages. Keep walking until 100 unique projects or the list stalls.
    for page in range(1, 16):
        if len(seen) >= limit:
            break
        url = (
            "https://api.geckoterminal.com/api/v2/networks/arc/pools"
            f"?page={page}&include=base_token&sort=h24_volume_usd_desc"
        )
        data = None
        for attempt in (1, 2):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode())
                break
            except Exception as exc:  # noqa: BLE001
                log.info("arc board page %s: %s", page, sanitize_exc(exc))
                if attempt == 1 and "429" in str(exc):
                    time.sleep(1.2)
                    continue
                data = None
                break
        if data is None:
            break
        time.sleep(0.35)
        rows = data.get("data") if isinstance(data, dict) else None
        if not rows:
            break
        before = len(seen)
        for row in rows:
            if not isinstance(row, dict):
                continue
            attr = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
            rel = row.get("relationships") if isinstance(row.get("relationships"), dict) else {}
            base = ((rel.get("base_token") or {}).get("data") or {})
            raw_id = str(base.get("id") or "")
            address = raw_id.split("_", 1)[-1] if raw_id.startswith("arc_") else ""
            if not (address.startswith("0x") and len(address) == 42):
                continue
            if address.lower() in (usdc, "0x" + "0" * 40):
                continue
            symbol = _symbol_from_pool_name(str(attr.get("name") or ""))
            if not symbol or symbol in _SKIP_SYMS:
                continue
            vol = attr.get("volume_usd") if isinstance(attr.get("volume_usd"), dict) else {}
            try:
                volume = float(vol.get("h24") or 0)
            except (TypeError, ValueError):
                volume = 0.0
            try:
                liq = float(attr.get("reserve_in_usd") or 0)
            except (TypeError, ValueError):
                liq = 0.0
            chg = attr.get("price_change_percentage") if isinstance(attr.get("price_change_percentage"), dict) else {}
            try:
                chg_h24 = float(chg.get("h24") or 0)
            except (TypeError, ValueError):
                chg_h24 = 0.0
            prev = seen.get(address.lower())
            if prev and float(prev.get("volume_h24") or 0) >= volume:
                continue
            tx = attr.get("transactions") if isinstance(attr.get("transactions"), dict) else {}
            seen[address.lower()] = _pool_row(
                attr,
                address=address,
                symbol=symbol,
                dex_id=str(((rel.get("dex") or {}).get("data") or {}).get("id") or ""),
                volume=volume,
                liq=liq,
                tx=tx,
                vol=vol,
            )
        if len(seen) == before and page > 1:
            break
    ranked = sorted(seen.values(), key=lambda r: float(r.get("volume_h24") or 0), reverse=True)
    for i, row in enumerate(ranked[:limit], start=1):
        row["rank"] = i
    return ranked[:limit]


def _num(raw: Any) -> float:
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def _age_sec(raw: str) -> float | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, time.time() - dt.timestamp())


def chain_young() -> bool:
    return time.time() - _ARC_PUBLIC.timestamp() < 24 * 3600


def _pool_row(
    attr: dict[str, Any],
    *,
    address: str,
    symbol: str,
    dex_id: str,
    volume: float,
    liq: float,
    tx: dict[str, Any],
    vol: dict[str, Any],
) -> dict[str, Any]:
    chg = attr.get("price_change_percentage") if isinstance(attr.get("price_change_percentage"), dict) else {}
    h1 = tx.get("h1") if isinstance(tx.get("h1"), dict) else {}
    m15 = tx.get("m15") if isinstance(tx.get("m15"), dict) else {}
    h24 = tx.get("h24") if isinstance(tx.get("h24"), dict) else {}
    return {
        "symbol": symbol,
        "address": address,
        "pool": str(attr.get("address") or ""),
        "liquidity_usdc": round(liq, 2),
        "volume_h24": round(volume, 2),
        "volume_h6": round(_num(vol.get("h6")), 2),
        "volume_h1": round(_num(vol.get("h1")), 2),
        "volume_m5": round(_num(vol.get("m5")), 2),
        "volume_m15": round(_num(vol.get("m15")), 2),
        "chg_h24": round(_num(chg.get("h24")), 2),
        "chg_h6": round(_num(chg.get("h6")), 2),
        "chg_h1": round(_num(chg.get("h1")), 2),
        "chg_m15": round(_num(chg.get("m15")), 2),
        "chg_m5": round(_num(chg.get("m5")), 2),
        "created_at": str(attr.get("pool_created_at") or ""),
        "buyers_h1": int(h1.get("buyers") or 0),
        "sellers_h1": int(h1.get("sellers") or 0),
        "buyers_m15": int(m15.get("buyers") or 0),
        "sellers_m15": int(m15.get("sellers") or 0),
        "txns_h24": int(h24.get("buys") or 0) + int(h24.get("sells") or 0),
        "dex": _dex_label(dex_id),
        "labels": ["v3"] if "v3" in dex_id else (["v4"] if "v4" in dex_id else []),
    }


def _gecko_pool_list(path: str, limit: int = 20, duration: str = "") -> list[dict[str, Any]]:
    """Gecko pool list. path is new_pools or trending_pools."""
    extra = f"&duration={duration}" if duration in ("5m", "1h", "6h", "24h") else ""
    url = (
        "https://api.geckoterminal.com/api/v2/networks/arc/"
        f"{path}?page=1&include=base_token,dex{extra}"
    )
    headers = {"Accept": "application/json", "User-Agent": "miki-desk-dex/1.0"}
    usdc = (
        os.environ.get("ARC_USDC") or "0x3600000000000000000000000000000000000000"
    ).lower()
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        log.info("arc %s: %s", path, sanitize_exc(exc))
        return []
    rows = data.get("data") if isinstance(data, dict) else None
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        attr = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
        rel = row.get("relationships") if isinstance(row.get("relationships"), dict) else {}
        base = ((rel.get("base_token") or {}).get("data") or {})
        raw_id = str(base.get("id") or "")
        address = raw_id.split("_", 1)[-1] if raw_id.startswith("arc_") else ""
        if not (address.startswith("0x") and len(address) == 42):
            continue
        if address.lower() in (usdc, "0x" + "0" * 40):
            continue
        symbol = _symbol_from_pool_name(str(attr.get("name") or ""))
        if not symbol or symbol in _SKIP_SYMS:
            continue
        vol = attr.get("volume_usd") if isinstance(attr.get("volume_usd"), dict) else {}
        tx = attr.get("transactions") if isinstance(attr.get("transactions"), dict) else {}
        out.append(_pool_row(
            attr,
            address=address,
            symbol=symbol,
            dex_id=str(((rel.get("dex") or {}).get("data") or {}).get("id") or ""),
            volume=_num(vol.get("h24")),
            liq=_num(attr.get("reserve_in_usd")),
            tx=tx,
            vol=vol,
        ))
        if len(out) >= limit:
            break
    return out


def _gecko_new(limit: int = 20) -> list[dict[str, Any]]:
    """Newest Arc pools. This is the pre-leaderboard tape, not the pumped top."""
    return _gecko_pool_list("new_pools", limit)


def new_pools() -> list[dict[str, Any]]:
    global _NEW_POOLS
    ts, rows = _NEW_POOLS
    if rows and time.time() - ts < 45:
        return rows
    rows = _gecko_new()
    _NEW_POOLS = (time.time(), rows)
    return rows


def open_window(row: dict[str, Any]) -> bool:
    """3 to 15 minutes old. Younger is the sniper window. Older is a finished print."""
    age = _age_sec(str(row.get("created_at") or ""))
    if age is None:
        return False
    return 180 <= age <= 15 * 60


def trend_window() -> list[dict[str, Any]]:
    """1h trending names still inside the 3–15 minute window."""
    global _TREND_WINDOW
    ts, rows = _TREND_WINDOW
    if rows and time.time() - ts < 45:
        return rows
    rows = [r for r in _gecko_pool_list("trending_pools") if open_window(r)]
    _TREND_WINDOW = (time.time(), rows)
    return rows


def refresh_timelines() -> dict[str, list[dict[str, Any]]]:
    """Rotate one Dexscreener chip per call. Cache keeps 5M, 1H, 6H, 24H together."""
    global _TIMELINE_CURSOR
    duration = _DURATIONS[_TIMELINE_CURSOR % len(_DURATIONS)]
    _TIMELINE_CURSOR += 1
    ts, rows = _TIMELINE.get(duration, (0.0, []))
    if not rows or time.time() - ts >= 150:
        fresh = _gecko_pool_list("trending_pools", limit=5, duration=duration)
        if fresh or duration not in _TIMELINE:
            _TIMELINE[duration] = (time.time(), fresh)
    return {d: list(_TIMELINE.get(d, (0.0, []))[1]) for d in _DURATIONS}


def _cached_pool_rows() -> list[dict[str, Any]]:
    """Pools already fetched this cycle. No extra request."""
    seen: dict[str, dict[str, Any]] = {}
    for row in list(_BOARD[1]) + list(_NEW_POOLS[1]) + list(_TREND_WINDOW[1]):
        addr = str(row.get("address") or "").lower()
        if addr and addr not in seen:
            seen[addr] = row
    return list(seen.values())


def _lead(rows: list[dict[str, Any]], duration: str) -> dict[str, Any] | None:
    key = _CHG_KEY[duration]
    best: dict[str, Any] | None = None
    best_chg = 0.0
    for row in rows:
        chg = _num(row.get(key))
        if best is None or chg > best_chg:
            best = row
            best_chg = chg
    return best


def timeline_line(books: dict[str, list[dict[str, Any]]] | None = None) -> str:
    """Desk line for every timeframe. Watch only. Does not arm a buy."""
    books = books if books is not None else refresh_timelines()
    cached = _cached_pool_rows()
    bits = []
    for duration in _DURATIONS:
        rows = books.get(duration) or []
        label = duration.upper()
        top = rows[0] if rows else _lead(cached, duration)
        if not top:
            bits.append(f"{label} unread")
            continue
        chg = _num(top.get(_CHG_KEY[duration]))
        bits.append(f"{label} ${top.get('symbol') or '?'} {chg:+.0f}%")
    return " · ".join(bits) + " · watch only"


def ranked_board(limit: int = 0) -> list[dict[str, Any]]:
    """Top Arc projects by 24h volume, capped at 100. Cached. Not a buy by itself."""
    global _BOARD
    limit = limit or _board_limit()
    ts, rows = _BOARD
    ttl = float(os.environ.get("ARC_DEX_BOARD_TTL", "60"))
    if rows and time.time() - ts < ttl:
        return rows[:limit]
    rows = _gecko_ranked(limit)
    _BOARD = (time.time(), rows)
    return rows[:limit]


_KLINE: dict[str, tuple[float, dict[str, Any]]] = {}


def kline_bias(pool: str) -> dict[str, Any]:
    """Last hourly candles. A 429 is a miss, not a veto. Unread is not a buy signal."""
    pool = (pool or "").strip().lower()
    if not (pool.startswith("0x") and len(pool) == 42):
        return {"ok": False, "bias": "unread"}
    hit = _KLINE.get(pool)
    if hit and hit[1].get("ok") and time.time() - hit[0] < float(os.environ.get("ARC_KLINE_TTL", "120")):
        return hit[1]
    if hit and not hit[1].get("ok") and time.time() - hit[0] < 20:
        return hit[1]
    url = (
        "https://api.geckoterminal.com/api/v2/networks/arc/pools/"
        f"{pool}/ohlcv/hour?aggregate=1&limit=8"
    )
    out: dict[str, Any] = {"ok": False, "bias": "unread", "bars": 0}
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "miki-desk-dex/1.0"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode())
        bars = ((payload.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        closes = []
        lows = []
        for bar in bars:
            if isinstance(bar, list) and len(bar) >= 5:
                lows.append(float(bar[3]))
                closes.append(float(bar[4]))
        if len(closes) >= 2 and closes[0] > 0 and lows:
            move = (closes[-1] - closes[0]) / closes[0]
            stretch = closes[-1] / min(lows) if min(lows) > 0 else 1.0
            bias = "up" if move > 0.02 else "down" if move < -0.02 else "flat"
            out = {
                "ok": True,
                "bias": bias,
                "bars": len(closes),
                "move": round(move, 3),
                "stretch": round(stretch, 3),
            }
    except Exception as exc:  # noqa: BLE001
        log.info("kline %s: %s", pool[:10], sanitize_exc(exc))
        if "429" in str(exc):
            out["rate_limited"] = True
            return out
    _KLINE[pool] = (time.time(), out)
    return out


def board_check(row: dict[str, Any]) -> dict[str, Any]:
    """Contract, volume, buyer tape, kline. Not a signed swap."""
    address = str(row.get("address") or "")
    onchain = token_symbol(address)
    buyers = int(row.get("buyers_h1") or 0)
    sellers = int(row.get("sellers_h1") or 0)
    age = _age_sec(str(row.get("created_at") or ""))
    if age is not None and age <= 15 * 60:
        # No hour candle exists inside the buy window. Do not spend a Gecko call on it.
        kline = {"ok": False, "bias": "unread", "bars": 0, "too_young": True}
    else:
        kline = kline_bias(str(row.get("pool") or ""))
    dex = str(row.get("dex") or "dex")
    return {
        "contract_ok": bool(onchain),
        "symbol_onchain": onchain,
        "dex": dex,
        "volume_h24": float(row.get("volume_h24") or 0),
        "buyers_h1": buyers,
        "sellers_h1": sellers,
        "txns_h24": int(row.get("txns_h24") or 0),
        "kline": kline.get("bias") or "unread",
        "kline_stretch": float(kline.get("stretch") or 0),
        "kline_rate_limited": bool(kline.get("rate_limited")),
        "kline_too_young": bool(kline.get("too_young")),
        "tradable": dex == "uniswap",
    }


_BOOSTS: tuple[float, set[str]] = (0.0, set())


def boosted_addresses() -> set[str]:
    """Paid Dexscreener boosts. A $100 tag is an ad, not a setup."""
    global _BOOSTS
    ts, addrs = _BOOSTS
    if addrs and time.time() - ts < 600:
        return addrs
    found: set[str] = set()
    headers = {"Accept": "application/json", "User-Agent": "miki-desk-dex/1.0"}
    for url in (
        "https://api.dexscreener.com/token-boosts/top/v1",
        "https://api.dexscreener.com/token-boosts/latest/v1",
    ):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=6) as resp:
                rows = json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001
            log.info("boost list: %s", sanitize_exc(exc))
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("chainId") or "").lower() != "arc":
                continue
            addr = str(row.get("tokenAddress") or "").strip().lower()
            if addr.startswith("0x") and len(addr) == 42:
                found.add(addr)
    _BOOSTS = (time.time(), found)
    return found


def trade_block(row: dict[str, Any] | None, check: dict[str, Any] | None = None) -> str:
    """
    Empty string means the tape is allowed to reach a quote.
    Ads, extended leaders, thin pools, and unread routers never get a buy.
    """
    if not row:
        return "not on arc board"
    dex = str(row.get("dex") or "")
    if dex != "uniswap":
        return f"{dex or 'dex'} · route unread"
    if str(row.get("address") or "").lower() in boosted_addresses():
        return "paid boost"
    try:
        pump = float(os.environ.get("ARC_SKIP_PUMP_H24", "200"))
    except ValueError:
        pump = 200.0
    age = _age_sec(str(row.get("created_at") or ""))
    young = chain_young() or (age is not None and age < 24 * 3600)
    if age is not None and age < 180:
        return "too new · sniper window"
    if not young and float(row.get("chg_h24") or 0) >= pump:
        return f"extended +{float(row.get('chg_h24') or 0):.0f}% 24h"
    if young:
        # Launch day has no real 24h. A running hour is the pump, not the first print.
        h1 = float(row.get("chg_h1") or 0)
        m15 = float(row.get("chg_m15") or 0)
        if h1 >= float(os.environ.get("ARC_SKIP_PUMP_H1", "80")):
            return f"already running +{h1:.0f}% 1h"
        if m15 >= float(os.environ.get("ARC_SKIP_PUMP_M15", "40")):
            return f"already running +{m15:.0f}% 15m"
    liq = float(row.get("liquidity_usdc") or 0)
    try:
        size = float(os.environ.get("ARC_BET_HARD_CAP", "0.3"))
    except ValueError:
        size = 0.3
    if liq <= 0 or size / liq > 0.01:
        return "pool too thin"
    buyers = int(row.get("buyers_m15") or row.get("buyers_h1") or 0)
    sellers = int(row.get("sellers_m15") or row.get("sellers_h1") or 0)
    if buyers <= sellers:
        return "sellers ≥ buyers"
    vol_m5 = float(row.get("volume_m5") or 0)
    vol_m15 = float(row.get("volume_m15") or 0)
    if vol_m15 <= 0 or (vol_m5 / 5.0) <= (vol_m15 / 15.0):
        return "volume not expanding"
    if check is not None:
        if not check.get("contract_ok"):
            return "contract unread"
        stretch = float((check.get("kline_stretch") or 0))
        unread = str(check.get("kline") or "") == "unread"
        # A rate limit, or no hour bar yet, is not a setup failure.
        if unread and not check.get("kline_rate_limited") and not check.get("kline_too_young"):
            return "kline unread"
        if stretch > float(os.environ.get("ARC_MAX_STRETCH", "1.8")):
            return f"extended {stretch:.1f}x from hour low"
    return ""


def board_row_for(address: str) -> dict[str, Any] | None:
    key = (address or "").strip().lower()
    if not key:
        return None
    for row in ranked_board():
        if str(row.get("address") or "").strip().lower() == key:
            return row
    return None


def quiet_reason(limit: int = 12) -> str:
    """Why no new pool armed. The volume board is a watchlist, not a buy source."""
    del limit
    rows = list(new_pools()) + list(trend_window())
    counts: Counter[str] = Counter()
    for row in rows:
        why = trade_block(row) or "tape clear"
        counts[why.split("·")[0].strip()[:32]] += 1
    top = counts.most_common(2)
    if not top:
        return "new pools unread"
    return " · ".join(f"{n}× {reason}" for reason, n in top)


def pick_ranked_target(exclude: set[str] | None = None) -> dict[str, Any] | None:
    """Next Uniswap setup inside the 3–15 minute window only."""
    held = {a.lower() for a in (exclude or set()) if a}

    def _take(row: dict[str, Any]) -> dict[str, Any] | None:
        addr = str(row.get("address") or "").lower()
        if addr and addr in held:
            return None
        if trade_block(row):
            return None
        check = board_check(row)
        reason = trade_block(row, check)
        if reason:
            return None
        out = dict(row)
        out["check"] = check
        return out

    seen: set[str] = set()
    for row in list(new_pools()) + list(trend_window()):
        addr = str(row.get("address") or "").lower()
        if addr in seen or not open_window(row):
            continue
        seen.add(addr)
        picked = _take(row)
        if picked:
            picked["early"] = True
            return picked
    return None


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
        labels = row.get("labels") if isinstance(row.get("labels"), list) else []
        out.append({
            "symbol": str(base.get("symbol") or ""),
            "address": str(base.get("address") or ""),
            "liquidity_usdc": round(_liq(row), 2),
            "dex": str(row.get("dexId") or ""),
            "labels": [str(x) for x in labels],
        })
    return out
