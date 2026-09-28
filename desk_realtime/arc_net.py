"""Arc mainnet network helpers — prefer local `cast`, fallback to JSON-RPC httpx.

Primary RPC + backup failover. Wallet USDC balance (fake floor OFF on mainnet).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from desk_realtime.foundry_bin import (
    ensure_foundry_on_path,
    get_chain_id,
    get_rpc_url,
    ordered_rpc_endpoints,
    rpc_endpoints,
    tool_path,
)
from desk_realtime.secrets import redact_text, sanitize_exc

log = logging.getLogger("arc_net")

# Sticky healthy RPC — Mac topbar stays smooth when primary stalls
_RPC_LOCK = threading.Lock()
_ACTIVE_RPC: str = ""
_RPC_FAILS: dict[str, int] = {}
_RPC_TIMEOUT = float(os.environ.get("ARC_RPC_TIMEOUT", "2"))
_RPC_FAIL_FLIP = int(os.environ.get("ARC_RPC_FAIL_FLIP", "1"))


def _rpc() -> str:
    with _RPC_LOCK:
        return _ACTIVE_RPC or get_rpc_url()


def probe_height_or_flip() -> str:
    """If sticky RPC does not return block height within ARC_RPC_TIMEOUT, flip.

    Detection takes the timeout (default 2s). The next execution URL is selected
    immediately after that — there is no extra sleep, and no plaintext HTTP hop.
    """
    import httpx

    primary = _rpc()
    timeout = max(0.4, float(_RPC_TIMEOUT))
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                primary,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_blockNumber",
                    "params": [],
                },
            )
            resp.raise_for_status()
            data = resp.json()
        if data.get("result") in (None, ""):
            raise RuntimeError("height missing")
        _mark_rpc_ok(primary)
        return primary
    except Exception as exc:  # noqa: BLE001
        log.info("height stall %s: %s", primary, sanitize_exc(exc))
        _mark_rpc_fail(primary)
        return _rpc()


def _chain_id() -> int:
    return get_chain_id()


def _mark_rpc_ok(url: str) -> None:
    global _ACTIVE_RPC
    with _RPC_LOCK:
        _ACTIVE_RPC = url
        _RPC_FAILS[url] = 0


def _mark_rpc_fail(url: str) -> None:
    """One silent height probe flips execution off the stalled node."""
    global _ACTIVE_RPC
    with _RPC_LOCK:
        _RPC_FAILS[url] = int(_RPC_FAILS.get(url) or 0) + 1
        current = _ACTIVE_RPC or get_rpc_url()
        if _RPC_FAILS[url] >= max(1, _RPC_FAIL_FLIP) and current == url:
            alts = [u for u in rpc_endpoints() if u != url]
            if alts:
                _ACTIVE_RPC = alts[0]
                log.warning("RPC sticky flip → %s (height stall on %s)", _ACTIVE_RPC, url)


def _endpoint_order() -> list[str]:
    with _RPC_LOCK:
        prefer = _ACTIVE_RPC or get_rpc_url()
    return ordered_rpc_endpoints(prefer)


class RpcFailover:
    """Sticky primary/backup/extra HTTPS pool. HTTP hosts are never used.

    Verified 2026-09-16:
      https://rpc.arc-scan.org → chain 5042
      https://arc-scan.org     → explorer, not JSON-RPC
      http://niorfun.com       → HTTP 500, not an Arc node. Do not send txs there.
    """

    REJECTED = ("http://niorfun.com", "https://niorfun.com", "https://arc-scan.org")

    def order(self) -> list[str]:
        return _endpoint_order()

    def mark_ok(self, url: str) -> None:
        _mark_rpc_ok(url)

    def mark_fail(self, url: str) -> None:
        _mark_rpc_fail(url)


# Back-compat snapshots (prefer _rpc() / _chain_id() at call time)
ARC_RPC_URL = get_rpc_url()
ARC_CHAIN_ID = get_chain_id()

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"block": None, "ts": 0.0, "err": "", "source": "", "rpc": ""}
_BAL_CACHE: dict[str, Any] = {"wei": None, "ts": 0.0, "addr": ""}
_TTL = float(os.environ.get("ARC_BLOCK_TTL", "2"))
_BAL_TTL = float(os.environ.get("ARC_BALANCE_TTL", "5.0"))

# Desk wallet (public address only — never put private keys here)
ARC_WALLET = (os.environ.get("ARC_WALLET") or "").strip()
# Default OFF on mainnet — show real USDC only.
ARC_FAKE_BALANCE_USDC = float(os.environ.get("ARC_FAKE_BALANCE_USDC", "1000"))
ARC_FAKE_WHEN_ZERO = os.environ.get("ARC_FAKE_WHEN_ZERO", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# Default OFF — mainnet requires funded wallet + ARC_PRIVATE_KEY.
ARC_FORCE_BROADCAST = os.environ.get("ARC_FORCE_BROADCAST", "0").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
    "",
)
ARC_SKIP_BALANCE_CHECK = os.environ.get("ARC_SKIP_BALANCE_CHECK", "0").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
    "",
)


def _cast_block_number(timeout: float | None = None) -> int:
    ensure_foundry_on_path()
    cast = tool_path("cast")
    if cast is None:
        raise FileNotFoundError("cast binary not found in project root or PATH")
    timeout = float(timeout if timeout is not None else _RPC_TIMEOUT)
    last_err = ""
    for rpc in _endpoint_order():
        try:
            proc = subprocess.run(
                [str(cast), "block-number", "--rpc-url", rpc],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            _mark_rpc_fail(rpc)
            last_err = f"cast timeout @ {rpc}"
            continue
        if proc.returncode == 0:
            line = (proc.stdout or "").strip().splitlines()[-1]
            _mark_rpc_ok(rpc)
            return int(line, 0)
        _mark_rpc_fail(rpc)
        last_err = (proc.stderr or proc.stdout or "cast failed").strip()
    raise RuntimeError(last_err[:200] or "cast block-number failed")


def _http_block_number(timeout: float | None = None) -> int:
    """JSON-RPC eth_blockNumber — sticky primary then seamless backup."""
    import httpx

    timeout = float(timeout if timeout is not None else _RPC_TIMEOUT)
    last_err: Exception | None = None
    for rpc in _endpoint_order():
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(
                    rpc,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_blockNumber",
                        "params": [],
                    },
                )
                resp.raise_for_status()
                data = resp.json()
            result = data.get("result")
            if result is None:
                raise RuntimeError(f"rpc error: {data.get('error') or data}")
            _mark_rpc_ok(rpc)
            return int(result, 16)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            _mark_rpc_fail(rpc)
            log.info("rpc %s block failed: %s", rpc, sanitize_exc(exc))
    raise RuntimeError(sanitize_exc(last_err) if last_err else "all RPCs failed")


def _http_get_balance(address: str, timeout: float | None = None) -> int:
    import httpx

    timeout = float(timeout if timeout is not None else _RPC_TIMEOUT)
    last_err: Exception | None = None
    for rpc in _endpoint_order():
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(
                    rpc,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_getBalance",
                        "params": [address, "latest"],
                    },
                )
                resp.raise_for_status()
                data = resp.json()
            result = data.get("result")
            if result is None:
                raise RuntimeError(f"rpc error: {data.get('error') or data}")
            _mark_rpc_ok(rpc)
            return int(result, 16)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            _mark_rpc_fail(rpc)
    raise RuntimeError(sanitize_exc(last_err) if last_err else "balance rpc failed")


def _cast_balance(address: str, timeout: float | None = None) -> int:
    ensure_foundry_on_path()
    cast = tool_path("cast")
    if cast is None:
        raise FileNotFoundError("cast missing")
    timeout = float(timeout if timeout is not None else _RPC_TIMEOUT)
    last_err = ""
    for rpc in _endpoint_order():
        try:
            proc = subprocess.run(
                [str(cast), "balance", address, "--rpc-url", rpc],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            _mark_rpc_fail(rpc)
            last_err = f"cast balance timeout @ {rpc}"
            continue
        if proc.returncode == 0:
            line = (proc.stdout or "").strip().splitlines()[-1]
            _mark_rpc_ok(rpc)
            return int(line, 0)
        _mark_rpc_fail(rpc)
        last_err = (proc.stderr or proc.stdout or "cast balance failed").strip()
    raise RuntimeError(last_err[:200] or "cast balance failed")


def fetch_wallet_usdc(
    address: str | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """
    Native USDC balance (wei, 18 decimals on Arc gas accounting).

    If chain balance is 0 (or read fails) and ARC_FAKE_WHEN_ZERO=1, pad
    `display_usdc` to ARC_FAKE_BALANCE_USDC for the desk scoreboard.
    Never blocks live broadcast — balance is display-only.
    """
    addr = (address or ARC_WALLET).strip()
    now = time.time()
    wei: int | None = None
    source = ""
    err = ""
    with _CACHE_LOCK:
        if (
            not force
            and _BAL_CACHE.get("wei") is not None
            and _BAL_CACHE.get("addr") == addr
            and (now - float(_BAL_CACHE.get("ts") or 0)) < _BAL_TTL
        ):
            wei = int(_BAL_CACHE["wei"])
            source = str(_BAL_CACHE.get("source") or "cache")

    if wei is None:
        try:
            wei = _cast_balance(addr)
            source = "cast"
        except Exception as exc:  # noqa: BLE001
            err = sanitize_exc(exc)
            try:
                wei = _http_get_balance(addr)
                source = "rpc"
                err = ""
            except Exception as exc2:  # noqa: BLE001
                err = sanitize_exc(exc2)
                wei = 0
                source = "error"

        with _CACHE_LOCK:
            _BAL_CACHE["wei"] = int(wei)
            _BAL_CACHE["ts"] = now
            _BAL_CACHE["addr"] = addr
            _BAL_CACHE["source"] = source

    raw_usdc = float(wei) / 1e18
    faked = False
    display = raw_usdc
    if ARC_FAKE_WHEN_ZERO and raw_usdc <= 0:
        display = float(ARC_FAKE_BALANCE_USDC)
        faked = True

    return {
        "ok": source != "error" or faked,
        "address": addr,
        "wei": int(wei),
        "raw_usdc": raw_usdc,
        "display_usdc": display,
        "faked": faked,
        "fake_floor": float(ARC_FAKE_BALANCE_USDC),
        "source": source,
        "rpc": _rpc(),
        "chain_id": _chain_id(),
        "skip_balance_check": ARC_SKIP_BALANCE_CHECK,
        "force_broadcast": ARC_FORCE_BROADCAST,
        "error": err,
    }


_DAY_PATH = Path(__file__).resolve().parents[1] / "grok-trading-desk" / "logs" / "wallet_day.json"
_FUND_PATH = Path(__file__).resolve().parents[1] / "grok-trading-desk" / "logs" / "funding_epoch.json"


def wallet_day_curve(address: str | None = None) -> dict[str, Any]:
    """Today's native USDC against the local clock.

    Points come from historical eth_getBalance, not a flat copy of the
    current balance drawn back to midnight. The file is a cache; the last
    point is always the live wallet when that read is real.
    """
    now = datetime.now().astimezone()
    day = now.date().isoformat()
    cached: dict[str, Any] = {}
    try:
        if _DAY_PATH.is_file():
            cached = json.loads(_DAY_PATH.read_text())
    except Exception:
        cached = {}
    points = list(cached.get("points") or []) if cached.get("date") == day else []
    live = fetch_wallet_usdc(address)
    raw = float(live.get("raw_usdc") or 0.0)
    if not live.get("faked"):
        now_ts = now.timestamp()
        if not points or abs(float(points[-1].get("usdc") or 0) - raw) >= 1e-6:
            points.append({"ts": now_ts, "usdc": raw})
        else:
            points[-1] = {"ts": now_ts, "usdc": raw}
    return {
        "date": day,
        "points": points,
        "now_usdc": raw,
        "faked": bool(live.get("faked")),
    }


def funding_mark() -> dict[str, Any]:
    """First non-zero wallet the desk actually saw. Survives restarts. Never invented."""
    try:
        if _FUND_PATH.is_file():
            saved = json.loads(_FUND_PATH.read_text())
            if isinstance(saved, dict) and float(saved.get("funded_at") or 0) > 0:
                return saved
    except Exception:
        saved = {}
    ts = 0.0
    usdc = 0.0
    try:
        if _DAY_PATH.is_file():
            curve = json.loads(_DAY_PATH.read_text())
            for pt in curve.get("points") or []:
                amt = float(pt.get("usdc") or 0)
                when = float(pt.get("ts") or 0)
                if amt > 0 and when > 0 and (ts <= 0 or when < ts):
                    ts, usdc = when, amt
    except Exception:
        ts = 0.0
    if ts <= 0:
        return {}
    row = {"funded_at": ts, "funded_usdc": round(usdc, 6), "source": "wallet_day"}
    try:
        _FUND_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FUND_PATH.write_text(json.dumps(row), encoding="utf-8")
    except Exception:
        pass
    return row


def cast_send(
    *,
    to: str,
    value_wei: int = 0,
    sig: str | None = None,
    args: list[str] | None = None,
    data: str | None = None,
    private_key: str | None = None,
    timeout: float = 60.0,
    anti_snipe: bool = True,
    priority_gwei: str | None = None,
    gas_price_gwei: str | None = None,
) -> dict[str, Any]:
    """
    Broadcast via local `cast send`. No balance preflight.
    anti_snipe=True → priority / gas price × 1.10 (抢跑建仓).
    Tries primary RPC then backup on failure.
    `data` is raw calldata for Universal Router / approval txs. Never log the key.
    """
    ensure_foundry_on_path()
    cast = tool_path("cast")
    pk = (private_key or os.environ.get("ARC_PRIVATE_KEY") or "").strip()
    if not pk:
        return {"ok": False, "error": "ARC_PRIVATE_KEY not set", "tx_id": ""}
    if cast is None:
        return {"ok": False, "error": "cast binary missing", "tx_id": ""}
    raw_data = (data or "").strip()
    if raw_data and (not raw_data.startswith("0x") or len(raw_data) < 10):
        return {"ok": False, "error": "invalid calldata", "tx_id": ""}
    if raw_data and sig:
        return {"ok": False, "error": "cast send refuses sig+data together", "tx_id": ""}

    if anti_snipe and (priority_gwei is None or gas_price_gwei is None):
        try:
            from desk_realtime.arc_strategy import bumped_gas_price_gwei, bumped_priority_gwei

            priority_gwei = priority_gwei or bumped_priority_gwei()
            gas_price_gwei = gas_price_gwei or bumped_gas_price_gwei()
        except Exception:
            priority_gwei = priority_gwei or os.environ.get("ARC_PRIORITY_GAS_PRICE", "22gwei")
            gas_price_gwei = gas_price_gwei or os.environ.get("ARC_GAS_PRICE", "22gwei")
    else:
        priority_gwei = priority_gwei or os.environ.get("ARC_PRIORITY_GAS_PRICE", "20gwei")
        gas_price_gwei = gas_price_gwei or os.environ.get("ARC_GAS_PRICE", "20gwei")

    last: dict[str, Any] = {"ok": False, "error": "no rpc", "tx_id": ""}
    probe_height_or_flip()
    for rpc in _endpoint_order():
        cmd = [str(cast), "send", to]
        if sig:
            cmd.append(sig)
            cmd.extend(str(a) for a in (args or []))
        if raw_data:
            cmd.extend(["--data", raw_data])
        cmd.extend(
            [
                "--value",
                str(int(value_wei)),
                "--rpc-url",
                rpc,
                "--private-key",
                pk,
                "--chain",
                str(_chain_id()),
                "--priority-gas-price",
                str(priority_gwei),
                "--gas-price",
                str(gas_price_gwei),
            ]
        )
        log.info(
            "cast send → %s value_wei=%s sig=%s data=%s prio=%s anti_snipe=%s rpc=%s",
            to[:12],
            value_wei,
            sig or "(plain)",
            f"{len(raw_data)}b" if raw_data else "none",
            priority_gwei,
            anti_snipe,
            rpc,
        )
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001
            _mark_rpc_fail(rpc)
            last = {"ok": False, "error": sanitize_exc(exc), "tx_id": "", "rpc": rpc}
            continue

        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        tx_id = ""
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("0x") and len(s) >= 66:
                tx_id = s.split()[0]
                break
        ok = proc.returncode == 0 and bool(tx_id)
        err = "" if ok else redact_text(out[-400:] if out else f"cast exit {proc.returncode}")
        if ok:
            _mark_rpc_ok(rpc)
        else:
            _mark_rpc_fail(rpc)
        last = {
            "ok": ok,
            "tx_id": tx_id,
            "error": err,
            "returncode": proc.returncode,
            "source": "cast",
            "priority_gwei": priority_gwei,
            "anti_snipe": anti_snipe,
            "rpc": rpc,
            "chain_id": _chain_id(),
        }
        if ok:
            return last
    return last


def fetch_arc_block_number(*, force: bool = False) -> dict[str, Any]:
    """Latest Arc block. Cache 2s. A silent height probe flips the sticky RPC."""
    now = time.time()
    with _CACHE_LOCK:
        age = now - float(_CACHE.get("ts") or 0)
        if not force and _CACHE.get("block") is not None and age < _TTL:
            return {
                "ok": True,
                "block": int(_CACHE["block"]),
                "cached": True,
                "age": age,
                "source": str(_CACHE.get("source") or ""),
                "rpc": _rpc(),
                "chain_id": _chain_id(),
                "error": "",
            }

    err = ""
    block: int | None = None
    source = ""
    try:
        block = _cast_block_number()
        source = "cast"
    except Exception as exc:  # noqa: BLE001
        err = sanitize_exc(exc)
        log.info("cast block-number failed, falling back to httpx: %s", err)
        try:
            block = _http_block_number()
            source = "rpc"
            err = ""
        except Exception as exc2:  # noqa: BLE001
            err = sanitize_exc(exc2)
            source = "error"

    with _CACHE_LOCK:
        if block is not None:
            _CACHE["block"] = block
            _CACHE["ts"] = now
            _CACHE["err"] = ""
            _CACHE["source"] = source
        else:
            _CACHE["err"] = err
            _CACHE["source"] = source

        return {
            "ok": block is not None,
            "block": int(block) if block is not None else (_CACHE.get("block") or 0),
            "cached": False,
            "age": 0.0,
            "source": source,
            "rpc": _rpc(),
            "chain_id": _chain_id(),
            "error": err,
        }


def format_block_label(info: dict[str, Any] | None) -> str:
    """Compact topbar value, e.g. 1,234,567 or —."""
    if not info or not info.get("ok"):
        if info and info.get("block"):
            return f"{int(info['block']):,}"
        return "—"
    return f"{int(info['block']):,}"
