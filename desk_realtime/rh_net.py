"""Robinhood Chain (4663) network helpers. ETH gas. Arc stays separate."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from desk_realtime.secrets import sanitize_exc

log = logging.getLogger("rh_net")

_DAY_PATH = (
    Path(__file__).resolve().parents[1]
    / "grok-trading-desk"
    / "logs"
    / "wallet_day_rh.json"
)
_FUND_PATH = (
    Path(__file__).resolve().parents[1]
    / "grok-trading-desk"
    / "logs"
    / "funding_epoch_rh.json"
)

CHAIN_ID = 4663
DEFAULT_RPC = "https://rpc.mainnet.chain.robinhood.com"
TATUM_RPC = "https://robinhood-mainnet.gateway.tatum.io"
EXPLORER = "https://robinhoodchain.blockscout.com"
# Canonical WETH + USDG on RH Chain (Gecko / Uniswap v3 tape).
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"


def chain_id() -> int:
    try:
        return int(os.environ.get("RH_CHAIN_ID") or CHAIN_ID)
    except ValueError:
        return CHAIN_ID


def rpc_url() -> str:
    return (os.environ.get("RH_RPC_URL") or DEFAULT_RPC).strip() or DEFAULT_RPC


def tatum_api_key() -> str:
    return (os.environ.get("TATUM_API_KEY") or os.environ.get("TATUM_KEY") or "").strip()


def alchemy_rpc_url() -> str:
    key = (os.environ.get("RH_ALCHEMY_KEY") or os.environ.get("ALCHEMY_API_KEY") or "").strip()
    if not key:
        return ""
    base = (os.environ.get("RH_ALCHEMY_RPC") or "https://robinhood-mainnet.g.alchemy.com/v2").rstrip("/")
    return f"{base}/{key}"


def rpc_endpoints() -> list[str]:
    """Primary + backups. Tatum/Alchemy auto-append when keys present."""
    primary = rpc_url()
    backup = (os.environ.get("RH_BACKUP_RPC_URL") or "").strip()
    out: list[str] = []
    for u in (primary, backup, TATUM_RPC if tatum_api_key() else "", alchemy_rpc_url()):
        u = (u or "").strip()
        if u.startswith("https://") and u not in out:
            out.append(u)
    return out or [DEFAULT_RPC]


def _rpc_headers(url: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "miki-rh-desk/1.0",
    }
    # Tatum gateway requires x-api-key (URL alone is not enough).
    if "tatum.io" in url.lower():
        key = tatum_api_key()
        if key:
            headers["x-api-key"] = key
    return headers


def wallet() -> str:
    return (
        os.environ.get("RH_WALLET")
        or os.environ.get("ARC_WALLET")
        or ""
    ).strip()


def private_key() -> str:
    return (
        os.environ.get("RH_PRIVATE_KEY")
        or os.environ.get("ARC_PRIVATE_KEY")
        or ""
    ).strip()


def rpc_json(
    method: str,
    params: list[Any] | None = None,
    *,
    timeout: float = 3.0,
    max_endpoints: int | None = None,
) -> Any:
    """JSON-RPC with short per-endpoint timeout so UI never wedges on a dead hop."""
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or [],
    }).encode()
    last: Exception | None = None
    urls = rpc_endpoints()
    if max_endpoints is not None:
        urls = urls[: max(1, int(max_endpoints))]
    for url in urls:
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers=_rpc_headers(url),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode())
            if payload.get("error"):
                raise RuntimeError(str(payload["error"])[:160])
            return payload.get("result")
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.info("rh rpc %s %s: %s", method, url, sanitize_exc(exc))
    raise RuntimeError(sanitize_exc(last) if last else "rh rpc failed")


def _hex_quantity(value: str | int | None) -> str:
    """JSON-RPC quantity: 0x-prefixed, no leading zeros (Go RPC rejects 0x00…)."""
    if value in ("", None):
        return "0x0"
    if isinstance(value, int):
        n = int(value)
    else:
        s = str(value).strip().lower()
        if not s or s in ("0x", "0x0"):
            return "0x0"
        try:
            n = int(s, 16) if s.startswith("0x") else int(s)
        except ValueError:
            return "0x0"
    if n <= 0:
        return "0x0"
    return hex(n)


def eth_call(
    to: str,
    data: str,
    *,
    from_addr: str = "",
    value: str | int = "0x0",
    timeout: float = 3.0,
    max_endpoints: int | None = 2,
) -> str:
    tx: dict[str, Any] = {"to": to, "data": data}
    if from_addr:
        tx["from"] = from_addr
    if value not in ("", None):
        tx["value"] = _hex_quantity(value)
    raw = rpc_json(
        "eth_call",
        [tx, "latest"],
        timeout=timeout,
        max_endpoints=max_endpoints,
    )
    return raw if isinstance(raw, str) else ""


def preflight(*, timeout: float = 3.0) -> dict[str, Any]:
    """Verify chain 4663 and read native ETH + optional wallet."""
    out: dict[str, Any] = {
        "ok": False,
        "chain_id": 0,
        "rpc": rpc_url(),
        "block": 0,
        "wallet": wallet(),
        "eth": 0.0,
        "reason": "",
    }
    try:
        cid = int(rpc_json("eth_chainId", timeout=timeout, max_endpoints=2), 16)
        out["chain_id"] = cid
        if cid != chain_id():
            out["reason"] = f"chainId {cid} · expected {chain_id()}"
            return out
        out["block"] = int(rpc_json("eth_blockNumber", timeout=timeout, max_endpoints=2), 16)
        addr = wallet()
        if addr.startswith("0x") and len(addr) == 42:
            wei = int(
                rpc_json("eth_getBalance", [addr, "latest"], timeout=timeout, max_endpoints=2),
                16,
            )
            out["eth"] = wei / 1e18
        out["ok"] = True
        out["reason"] = "rpc ok"
    except Exception as exc:  # noqa: BLE001
        out["reason"] = sanitize_exc(exc)
    return out


def fetch_eth_balance(address: str | None = None, *, timeout: float = 3.0) -> dict[str, Any]:
    addr = (address or wallet()).strip()
    try:
        wei = int(
            rpc_json("eth_getBalance", [addr, "latest"], timeout=timeout, max_endpoints=2),
            16,
        )
        return {"ok": True, "address": addr, "wei": wei, "eth": wei / 1e18}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "address": addr, "wei": 0, "eth": 0.0, "error": sanitize_exc(exc)}


def broadcast_raw(signed_hex: str) -> dict[str, Any]:
    raw = signed_hex if str(signed_hex).startswith("0x") else f"0x{signed_hex}"
    try:
        tx_hash = rpc_json("eth_sendRawTransaction", [raw], timeout=30.0)
        return {"ok": True, "tx_id": str(tx_hash or ""), "error": ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "tx_id": "", "error": sanitize_exc(exc)}


def tx_receipt(tx_hash: str, *, timeout: float = 8.0) -> dict[str, Any]:
    """Receipt status. pending=True means unread or not mined, not a revert."""
    hx = str(tx_hash or "")
    if not (hx.startswith("0x") and len(hx) >= 66):
        return {"ok": False, "pending": False, "error": "tx hash short", "tx_id": hx, "gas_eth": 0.0}
    try:
        raw = rpc_json("eth_getTransactionReceipt", [hx], timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "pending": True, "error": sanitize_exc(exc), "tx_id": hx, "gas_eth": 0.0}
    if not isinstance(raw, dict):
        return {"ok": False, "pending": True, "error": "pending", "tx_id": hx, "gas_eth": 0.0}
    status = int(raw.get("status") or "0x0", 16)
    gas_used = int(raw.get("gasUsed") or "0x0", 16)
    price = int(raw.get("effectiveGasPrice") or "0x0", 16)
    return {
        "ok": status == 1,
        "pending": False,
        "status": status,
        "gas_eth": gas_used * price / 1e18,
        "tx_id": hx,
        "error": "" if status == 1 else "reverted",
    }


def wait_receipt(tx_hash: str, *, attempts: int = 8, pause: float = 2.0) -> dict[str, Any]:
    last: dict[str, Any] = {
        "ok": False,
        "pending": True,
        "error": "pending",
        "tx_id": tx_hash,
        "gas_eth": 0.0,
    }
    for _ in range(max(1, int(attempts))):
        last = tx_receipt(tx_hash)
        if not last.get("pending"):
            return last
        time.sleep(pause)
    return last


def _merge_equity_points(
    base: list[dict[str, Any]],
    extra: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge by timestamp; prefer tagged trade nodes when ts collide."""
    by_ts: dict[int, dict[str, Any]] = {}
    for p in base + extra:
        ts = float(p.get("ts") or 0.0)
        u = float(p.get("usdg") or 0.0)
        if ts <= 0 or u < 0:
            continue
        key = int(round(ts))
        prev = by_ts.get(key)
        if prev is None:
            by_ts[key] = dict(p)
            continue
        # Keep buy/sell tags over plain samples.
        if str(p.get("tag") or "") in ("buy", "sell", "fund") and str(prev.get("tag") or "") not in (
            "buy",
            "sell",
            "fund",
        ):
            by_ts[key] = dict(p)
        elif abs(u - float(prev.get("usdg") or 0)) >= 1e-6:
            by_ts[key] = dict(p)
    return [by_ts[k] for k in sorted(by_ts)]


def note_equity_after_trade(*, tag: str = "", when: float | None = None) -> None:
    """Force a curve node after a fill. Prefer NAV (cash + open marks), not cash cliffs."""
    ts = float(when or time.time())
    live = 0.0
    try:
        from desk_realtime.rh_uniswap import erc20_balance

        who = wallet()
        if who:
            live = float(erc20_balance(USDG, who, timeout=3.0)) / 1_000_000.0
    except Exception:
        live = 0.0
    # Add open-book marks so buys do not draw a fake −slot cliff on THE BALANCE.
    try:
        from desk_realtime.engine_state import read_engine_state

        eng = read_engine_state() or {}
        for pos in eng.get("open_book") or []:
            if not isinstance(pos, dict):
                continue
            entry = float(pos.get("entry_usdg") or 0.0)
            mult = float(pos.get("live_mult") or 0.0)
            if entry > 0 and mult > 0:
                live += entry * mult
    except Exception:
        pass
    if live <= 0:
        # Fall back to ledger replay tip.
        try:
            from desk_realtime.rh_ledger import cash_equity_curve

            mark = funding_mark()
            curve = cash_equity_curve(
                funded_at=float(mark.get("funded_at") or 0),
                funded_usdg=float(mark.get("funded_usdg") or 0),
            )
            if curve:
                live = float(curve[-1].get("usdg") or 0)
        except Exception:
            return
    if live <= 0:
        return
    # Route through NAV writer so cash-only tags cannot re-enter the file.
    try:
        wallet_day_curve(live, nav=True)
    except Exception as exc:  # noqa: BLE001
        log.debug("note_equity_after_trade: %s", sanitize_exc(exc))
        return
    # Keep a trade-tagged breadcrumb only when explicitly buy/sell (for audits).
    t = (tag or "").strip().lower()
    if t not in ("buy", "sell"):
        return
    try:
        cached: dict[str, Any] = {}
        if _DAY_PATH.is_file():
            cached = json.loads(_DAY_PATH.read_text(encoding="utf-8"))
        points = list(cached.get("points") or [])
        # Do not append cash cliffs — tip already updated via wallet_day_curve(nav=True).
        if points:
            points[-1] = {**points[-1], "tag": t, "trade_tag": t}
        mark = funding_mark(live)
        payload = {
            "funded_at": float(mark.get("funded_at") or cached.get("funded_at") or 0) or None,
            "date": datetime.now().astimezone().date().isoformat(),
            "points": points,
            "source": "nav",
        }
        _DAY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DAY_PATH.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.debug("note_equity_after_trade tag: %s", sanitize_exc(exc))


def wallet_day_curve(
    usdg: float | None = None,
    *,
    day_net: float | None = None,
    nav: bool = True,
) -> dict[str, Any]:
    """Session equity curve for THE BALANCE — NAV by default (cash + marks).

    Always writes NAV-shaped tips to wallet_day_rh.json. Cash-only samples
    (old live/fill writers) are stripped so the chart cannot sawtooth between
    wallet cash (~9.5 with 2 slots) and true NAV (~12.5).
    """
    nav = True  # THE BALANCE file is NAV-only; ignore callers that pass False.
    now = datetime.now().astimezone()
    day = now.date().isoformat()
    now_ts = now.timestamp()
    cached: dict[str, Any] = {}
    try:
        if _DAY_PATH.is_file():
            cached = json.loads(_DAY_PATH.read_text(encoding="utf-8"))
    except Exception:
        cached = {}
    # Keep history across days (legacy files wiped on date change — stop that).
    points: list[dict[str, Any]] = [
        p
        for p in list(cached.get("points") or [])
        if str(p.get("tag") or "") not in ("buy", "sell", "fill", "live")
    ]
    raw = float(usdg or 0.0)
    fund: dict[str, Any] = {}
    if raw > 0:
        try:
            fund = funding_mark(raw)
        except Exception:
            fund = {}
    fa = float(fund.get("funded_at") or cached.get("funded_at") or 0.0)
    fu = float(fund.get("funded_usdg") or 0.0)
    if fa > 0 and points:
        points = [p for p in points if float(p.get("ts") or 0) >= fa - 1.0]
    if raw > 0:
        # Anchor at funding once so the curve does not restart at local midnight.
        if fa > 0 and fu > 0:
            if not points or float(points[0].get("ts") or 0) > fa + 90:
                points.insert(0, {"ts": fa, "usdg": round(fu, 6), "tag": "fund"})
            elif abs(float(points[0].get("ts") or 0) - fa) <= 90:
                points[0] = {"ts": fa, "usdg": round(fu, 6), "tag": "fund"}
        elif not points and day_net is not None:
            # Cold start before funding_mark exists: approximate session open.
            open_est = max(0.0, raw - float(day_net))
            points.append({"ts": now_ts, "usdg": round(open_est, 6), "tag": "nav"})
        min_step = 0.01  # USDG — ignore dust jitter
        min_gap = 60.0  # seconds — keep last tip fresh without flooding
        # Prefer engine NAV if caller passed cash-only while book is open.
        try:
            from desk_realtime.engine_state import read_engine_state

            eng = read_engine_state() or {}
            cash = float(eng.get("wallet_usdg") or 0.0)
            mtm = 0.0
            for pos in eng.get("open_book") or []:
                if not isinstance(pos, dict):
                    continue
                entry = float(pos.get("entry_usdg") or 0.0)
                mult = float(pos.get("live_mult") or 0.0)
                if entry > 0 and mult > 0:
                    mtm += entry * mult
            eng_nav = cash + mtm
            if eng_nav > 0 and (raw <= 0 or (mtm > 0 and (eng_nav - raw) >= 1.0)):
                raw = eng_nav
        except Exception:
            pass
        # Reject funded-mark echo as a live tip (causes 12.58↔live sawtooth).
        if (
            fu > 0
            and abs(raw - fu) < 1e-6
            and points
            and str(points[-1].get("tag") or "") != "fund"
            and abs(float(points[-1].get("usdg") or 0.0) - fu) >= min_step
        ):
            raw = float(points[-1].get("usdg") or raw)
        # Reject cash-only cliffs vs last NAV tip.
        if points:
            last_nav = next(
                (
                    float(p.get("usdg") or 0.0)
                    for p in reversed(points)
                    if str(p.get("tag") or "") in ("nav", "fund")
                    and float(p.get("usdg") or 0) > 0
                ),
                0.0,
            )
            if last_nav > 0 and (last_nav - raw) >= 1.2:
                raw = last_nav
        if not points:
            points.append({"ts": now_ts, "usdg": round(raw, 6), "tag": "nav"})
        else:
            last = points[-1]
            last_u = float(last.get("usdg") or 0.0)
            last_ts = float(last.get("ts") or 0.0)
            if abs(last_u - raw) >= min_step:
                points.append({"ts": now_ts, "usdg": round(raw, 6), "tag": "nav"})
            elif now_ts - last_ts >= min_gap:
                points[-1] = {
                    **last,
                    "ts": last_ts,
                    "usdg": round(raw, 6),
                    "tag": "nav",
                    "seen": now_ts,
                }
        # Collapse leftover cliffs / funded ping-pong.
        if len(points) >= 3:
            cleaned: list[dict[str, Any]] = [points[0]]
            for p in points[1:]:
                u = float(p.get("usdg") or 0.0)
                prev_u = float(cleaned[-1].get("usdg") or 0.0)
                tag = str(p.get("tag") or "")
                if tag in ("buy", "sell", "fill", "live"):
                    continue
                if fu > 0 and abs(u - fu) < 1e-6 and abs(prev_u - fu) >= min_step:
                    continue
                if (prev_u - u) >= 1.2:
                    continue
                if abs(u - prev_u) < min_step:
                    cleaned[-1] = {
                        **cleaned[-1],
                        "usdg": round(u, 6),
                        "tag": tag or cleaned[-1].get("tag"),
                    }
                    continue
                cleaned.append(p)
            points = cleaned
        if len(points) > 800:
            head = points[:1]
            tail = points[1:][-799:]
            points = head + tail
        try:
            _DAY_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "funded_at": fa or None,
                "date": day,
                "points": points,
                "source": "nav",
            }
            _DAY_PATH.write_text(
                json.dumps(payload, separators=(",", ":")),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("wallet_day_rh write: %s", sanitize_exc(exc))
    open_usdg = float(points[0].get("usdg") or 0.0) if points else raw
    if fu > 0:
        open_usdg = fu
    return {
        "date": day,
        "funded_at": fa,
        "funded_usdg": fu,
        "points": points,
        "now_usdg": raw,
        "open_usdg": open_usdg,
        "daily": _daily_bars(points, fa, now, live_usdg=raw if raw > 0 else None),
        "nav": True,
    }


def _daily_bars(
    points: list[dict[str, Any]],
    funded_at: float,
    now: datetime,
    *,
    live_usdg: float | None = None,
) -> list[dict[str, Any]]:
    """TradingView-style 1D closes: one USDG per local calendar day from funding."""
    from datetime import timedelta

    by_day: dict[Any, float] = {}
    for p in points:
        ts = float(p.get("ts") or 0.0)
        u = float(p.get("usdg") or 0.0)
        if ts <= 0 or u <= 0:
            continue
        d = datetime.fromtimestamp(ts).astimezone().date()
        by_day[d] = u  # last sample that day = close
    if live_usdg is not None and live_usdg > 0:
        by_day[now.date()] = float(live_usdg)
    if funded_at > 0:
        d0 = datetime.fromtimestamp(funded_at).astimezone().date()
    elif by_day:
        d0 = min(by_day)
    else:
        return []
    d1 = now.date()
    if d1 < d0:
        d1 = d0
    out: list[dict[str, Any]] = []
    last: float | None = None
    cur = d0
    while cur <= d1:
        if cur in by_day:
            last = float(by_day[cur])
        if last is not None:
            out.append({"date": cur.isoformat(), "usdg": round(last, 6)})
        cur += timedelta(days=1)
    return out


def funding_mark(usdg: float | None = None) -> dict[str, Any]:
    """First RH USDG the desk actually saw. Survives restarts. Never invented."""
    try:
        if _FUND_PATH.is_file():
            saved = json.loads(_FUND_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict) and float(saved.get("funded_at") or 0) > 0:
                return saved
    except Exception:
        saved = {}

    ts = 0.0
    amt = 0.0
    # Prefer earliest live sample from the day curve (skip midnight seed).
    try:
        if _DAY_PATH.is_file():
            curve = json.loads(_DAY_PATH.read_text(encoding="utf-8"))
            for pt in curve.get("points") or []:
                u = float(pt.get("usdg") or 0)
                when = float(pt.get("ts") or 0)
                if u <= 0 or when <= 0:
                    continue
                # Midnight-seeded open is synthetic — skip exact local midnight.
                local = datetime.fromtimestamp(when).astimezone()
                if local.hour == 0 and local.minute == 0 and local.second == 0:
                    continue
                if ts <= 0 or when < ts:
                    ts, amt = when, u
    except Exception:
        ts, amt = 0.0, 0.0

    live = float(usdg or 0.0)
    if ts <= 0 and live > 0:
        ts, amt = time.time(), live
    if ts <= 0:
        return {}

    row = {
        "funded_at": ts,
        "funded_usdg": round(amt, 6),
        "source": "rh_wallet",
    }
    try:
        _FUND_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FUND_PATH.write_text(json.dumps(row), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.debug("funding_epoch_rh write: %s", sanitize_exc(exc))
    return row
