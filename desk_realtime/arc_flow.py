"""Watch the two known Warp curves before a live micro buy.

Tolly and DYOR listeners arm only when a real factory address is in env.
Placeholder CAs are never registered and never bought.

A live 0.5–1 USDC buy is allowed only when recent Transfer logs on that
curve's token show enough external buyers. No samples → no broadcast.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from typing import Any

from desk_realtime.foundry_bin import is_allowed_rpc, rpc_endpoints
from desk_realtime.secrets import sanitize_exc

log = logging.getLogger("arc_flow")

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TOKEN_SELECTOR = "0xfc0c546a"
_FAKE = {
    "0x1111111111111111111111111111111111111111",
    "0x0000000000000000000000000000000000000000",
    "0x0c9535416fd3b772646c4575e0664fd65afeeeee",
    "0xe2324ff2a59f8ecba8c321c6466e59121c00e775",
}
_CACHE: dict[str, dict[str, Any]] = {}
_PAD_STATUS: dict[str, str] = {}


def is_real_ca(address: str) -> bool:
    a = (address or "").strip().lower()
    if not (a.startswith("0x") and len(a) == 42):
        return False
    if a in _FAKE or len(set(a[2:])) <= 2:
        return False
    try:
        return int(a, 16) != 0
    except ValueError:
        return False


def watch_curves() -> list[str]:
    raw = os.environ.get("ARC_WARP_CAS", "")
    out: list[str] = []
    for part in raw.replace(";", ",").split(","):
        a = part.strip()
        if is_real_ca(a) and a.lower() not in out:
            out.append(a.lower())
    return out


def pad_listener_status() -> dict[str, str]:
    """Which launchpad factories are actually armed. No invented addresses."""
    status = {
        "warp": "armed" if is_real_ca(os.environ.get("ARC_WARP_FACTORY", "")) else "dark",
        "tolly": "armed" if is_real_ca(os.environ.get("ARC_TOLLY_FACTORY", "")) else "dark · no mainnet CA",
        "dyor": "armed" if is_real_ca(os.environ.get("ARC_DYOR_FACTORY", "")) else "dark · no mainnet CA",
    }
    _PAD_STATUS.update(status)
    return status


def _rpc(method: str, params: list[Any]) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    timeout = float(os.environ.get("ARC_RPC_TIMEOUT", "2"))
    last_err: Exception | None = None
    for url in rpc_endpoints():
        if not is_allowed_rpc(url):
            continue
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json", "User-Agent": "miki-arc-flow/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            if data.get("error"):
                raise RuntimeError(str(data["error"]))
            return data.get("result")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
    raise RuntimeError(sanitize_exc(last_err) if last_err else "rpc failed")


def _topic_addr(topic: str) -> str:
    if not isinstance(topic, str) or len(topic) < 66:
        return ""
    return "0x" + topic[-40:]


def _token_of(curve: str) -> str:
    try:
        result = _rpc("eth_call", [{"to": curve, "data": TOKEN_SELECTOR}, "latest"])
    except Exception:
        return ""
    if not isinstance(result, str) or len(result) < 66:
        return ""
    token = "0x" + result[-40:]
    return token.lower() if is_real_ca(token) else ""


def frenzy(curve: str, *, force: bool = False) -> dict[str, Any]:
    """External buyers on this curve's token in the lookback window."""
    key = (curve or "").lower()
    now = time.time()
    hit = _CACHE.get(key)
    if hit and not force and now - float(hit.get("ts") or 0) < 12:
        return hit

    need = max(1, int(os.environ.get("ARC_FLOW_MIN_WALLETS", "3")))
    lookback = max(50, int(os.environ.get("ARC_FLOW_LOOKBACK", "800")))
    wallet = (os.environ.get("ARC_WALLET") or "").strip().lower()
    smart_raw = os.environ.get("ARC_SMART_WALLETS", "")
    smart = {p.strip().lower() for p in smart_raw.replace(";", ",").split(",") if is_real_ca(p.strip())}
    base = {
        "ok": False,
        "hot": False,
        "buyers": 0,
        "reason": "no external buys",
        "ts": now,
        "curve": key,
    }
    if not is_real_ca(key):
        base["reason"] = "not a watched curve"
        _CACHE[key] = base
        return base
    try:
        latest = int(_rpc("eth_blockNumber", []), 16)
        token = _token_of(key)
        if not token:
            base["reason"] = "token() unread · no buy"
            _CACHE[key] = base
            return base
        logs = _rpc(
            "eth_getLogs",
            [{
                "address": token,
                "fromBlock": hex(max(0, latest - lookback)),
                "toBlock": hex(latest),
                "topics": [TRANSFER_TOPIC],
            }],
        ) or []
    except Exception as exc:  # noqa: BLE001
        base["reason"] = f"flow unread · {sanitize_exc(exc)}"
        _CACHE[key] = base
        return base

    buyers: set[str] = set()
    for entry in logs:
        if not isinstance(entry, dict):
            continue
        topics = entry.get("topics") or []
        if len(topics) < 3:
            continue
        sender = _topic_addr(topics[1]).lower()
        recv = _topic_addr(topics[2]).lower()
        # Bonding-curve buy: curve (or pair) sends tokens to a wallet.
        if sender != key and sender != token:
            continue
        if not is_real_ca(recv) or recv in (wallet, key, token):
            continue
        if smart and recv not in smart:
            continue
        buyers.add(recv)

    hot = len(buyers) >= need
    base.update(
        {
            "ok": True,
            "hot": hot,
            "buyers": len(buyers),
            "need": need,
            "reason": (
                f"external buyers {len(buyers)}/{need}"
                if hot
                else f"flow cold · buyers {len(buyers)}/{need}"
            ),
        }
    )
    _CACHE[key] = base
    return base


def live_buy_allowed(address: str, *, live: bool) -> dict[str, Any]:
    """Hard gate for robots 1–4. Dry-run is not blocked."""
    if not live:
        return {"ok": True, "reason": "dry-run", "buyers": 0}
    a = (address or "").strip().lower()
    watched = watch_curves()
    if a not in watched:
        return {
            "ok": False,
            "reason": "not one of the two Warp CAs · Tolly/DYOR not armed",
            "buyers": 0,
        }
    snap = frenzy(a)
    if not snap.get("hot"):
        return {"ok": False, "reason": snap.get("reason") or "flow cold", "buyers": snap.get("buyers") or 0}
    return {"ok": True, "reason": str(snap.get("reason")), "buyers": snap.get("buyers") or 0}


def refresh_watch() -> dict[str, Any]:
    """Background sample. Does not broadcast."""
    pads = pad_listener_status()
    snaps = [frenzy(c) for c in watch_curves()]
    return {"pads": pads, "curves": snaps}
