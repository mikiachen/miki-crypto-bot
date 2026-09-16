"""Warp (etc.) Factory event watcher — skeleton for mainnet new-curve discovery.

Wire when official Factory CA is published:

  ARC_WARP_FACTORY=0x...
  ARC_FACTORY_TOPIC0=0x...   # optional; default = TokenCreated(address,address,…) keccak
  ARC_FACTORY_POLL_SEC=8
  ARC_FACTORY_LOOKBACK=2000  # blocks

Until Factory is set, poll() is a no-op. Discovered curves are registered into
arc_launchpads via register_curve().
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from desk_realtime.secrets import sanitize_exc

log = logging.getLogger("arc_factory")

# Override with ARC_FACTORY_TOPIC0 when Warp publishes the real event signature.
# Left unset → eth_getLogs without topic filter (ok for low-volume Factory).
def factory_address() -> str:
    return (os.environ.get("ARC_WARP_FACTORY") or os.environ.get("ARC_FACTORY") or "").strip()


def topic0() -> str | None:
    t = (os.environ.get("ARC_FACTORY_TOPIC0") or "").strip()
    if t.startswith("0x") and len(t) >= 66:
        return t
    return None  # no filter → all Factory logs (heavier; set topic on mainnet)


def _rpc_call(method: str, params: list[Any]) -> Any:
    import json
    import urllib.request

    from desk_realtime.foundry_bin import rpc_endpoints

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last_err: Exception | None = None
    for url in rpc_endpoints():
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "miki-desk-factory/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=float(os.environ.get("ARC_RPC_TIMEOUT", "2"))) as resp:
                data = json.loads(resp.read().decode())
            if data.get("error"):
                raise RuntimeError(str(data["error"]))
            return data.get("result")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
    raise RuntimeError(sanitize_exc(last_err) if last_err else "rpc failed")


def _extract_curve_from_log(entry: dict[str, Any]) -> str | None:
    """Best-effort: first non-factory address in topics[1] or data word0."""
    topics = entry.get("topics") or []
    # indexed address often in topics[1]
    for t in topics[1:]:
        if isinstance(t, str) and len(t) >= 66:
            addr = "0x" + t[-40:]
            if int(addr, 16) != 0:
                return addr
    data = (entry.get("data") or "0x")[2:]
    if len(data) >= 64:
        addr = "0x" + data[24:64]
        if int(addr, 16) != 0:
            return addr
    return None


_LAST_BLOCK: int | None = None


def poll_new_curves(*, launchpad: str = "warp", factory: str | None = None) -> list[dict[str, Any]]:
    """
    eth_getLogs against Factory. Returns newly registered curve dicts.
    No-op when the factory address is missing or not a real CA.
    """
    global _LAST_BLOCK
    from desk_realtime.arc_flow import is_real_ca

    factory = (factory or factory_address()).strip()
    if not is_real_ca(factory):
        return []

    lookback = int(os.environ.get("ARC_FACTORY_LOOKBACK", "2000"))
    try:
        latest = int(_rpc_call("eth_blockNumber", []), 16)
    except Exception as exc:  # noqa: BLE001
        log.info("factory blockNumber failed: %s", sanitize_exc(exc))
        return []

    from_block = _LAST_BLOCK if _LAST_BLOCK is not None else max(0, latest - lookback)
    if from_block > latest:
        from_block = latest
    _LAST_BLOCK = latest

    try:
        filt: dict[str, Any] = {
            "address": factory,
            "fromBlock": hex(from_block),
            "toBlock": hex(latest),
        }
        t0 = topic0()
        if t0:
            filt["topics"] = [t0]
        logs = _rpc_call("eth_getLogs", [filt]) or []
    except Exception as exc:  # noqa: BLE001
        log.info("factory getLogs failed: %s", sanitize_exc(exc))
        return []

    from desk_realtime.arc_launchpads import register_curve

    found: list[dict[str, Any]] = []
    for entry in logs:
        curve = _extract_curve_from_log(entry if isinstance(entry, dict) else {})
        if not curve:
            continue
        tok = register_curve(curve, launchpad=launchpad)
        if tok:
            found.append(
                {
                    "curve": curve,
                    "symbol": tok.symbol,
                    "launchpad": tok.launchpad,
                    "tx": entry.get("transactionHash"),
                    "block": int(entry.get("blockNumber") or "0x0", 16),
                }
            )
            log.info("factory curve +%s %s", tok.symbol, curve[:12])
    return found


def poll_loop_once() -> list[dict[str, Any]]:
    """Warp factory plus Tolly/DYOR only when those factories are real CAs."""
    from desk_realtime.arc_flow import is_real_ca, refresh_watch

    found: list[dict[str, Any]] = []
    try:
        found.extend(poll_new_curves(launchpad="warp"))
    except Exception as exc:  # noqa: BLE001
        log.info("factory poll: %s", sanitize_exc(exc))
    for pad, key in (("tolly", "ARC_TOLLY_FACTORY"), ("dyor", "ARC_DYOR_FACTORY")):
        addr = (os.environ.get(key) or "").strip()
        if not is_real_ca(addr):
            continue
        try:
            found.extend(poll_new_curves(launchpad=pad, factory=addr))
        except Exception as exc:  # noqa: BLE001
            log.info("%s factory poll: %s", pad, sanitize_exc(exc))
    try:
        refresh_watch()
    except Exception as exc:  # noqa: BLE001
        log.info("flow watch: %s", sanitize_exc(exc))
    return found


def status() -> dict[str, Any]:
    from desk_realtime.arc_flow import pad_listener_status

    pads = pad_listener_status()
    return {
        "factory": factory_address() or None,
        "topic0": topic0(),
        "last_block": _LAST_BLOCK,
        "poll_sec": float(os.environ.get("ARC_FACTORY_POLL_SEC", "8")),
        "armed": pads.get("warp") == "armed",
        "pads": pads,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(status())
    print(poll_loop_once())
    time.sleep(0.1)
