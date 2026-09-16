#!/usr/bin/env python3
"""Arc desk preflight — verify RPC / chain / wallet / Warp CA before trading.

Usage:
  python3 scripts/arc_preflight.py
  python3 scripts/arc_preflight.py --mainnet   # requires .env.mainnet or env overrides

Exits 0 on pass, 1 on fail. Never prints private keys.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k, v = k.strip(), v.strip().strip("'").strip('"')
        if k and k not in os.environ:
            os.environ[k] = v


def _rpc(url: str, method: str, params: list) -> object:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "miki-preflight/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data.get("result")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mainnet", action="store_true", help="Prefer .env.mainnet overlays")
    args = ap.parse_args()

    _load_dotenv(ROOT / ".env")
    if args.mainnet:
        _load_dotenv(ROOT / ".env.mainnet")
        os.environ.setdefault("ARC_NETWORK", "mainnet")

    sys.path[:0] = [str(ROOT / "grok-trading-desk" / ".vendor"), str(ROOT)]

    rpc = (os.environ.get("ARC_RPC_URL") or "").strip()
    backup = (os.environ.get("ARC_BACKUP_RPC_URL") or rpc).strip()
    chain = int(os.environ.get("ARC_CHAIN_ID") or "0")
    wallet = (os.environ.get("ARC_WALLET") or "").strip()
    curve = (os.environ.get("ARC_WARP_CAS") or os.environ.get("ARC_DEFAULT_CURVE") or "").split(",")[0].strip()
    pads = (os.environ.get("ARC_PADS") or "warp").strip()
    factory = (os.environ.get("ARC_WARP_FACTORY") or "").strip()
    pk_set = bool((os.environ.get("ARC_PRIVATE_KEY") or "").strip())
    network = (os.environ.get("ARC_NETWORK") or ("mainnet" if args.mainnet else "testnet")).strip()

    print("══ Arc preflight ══")
    print(f"  network   {network}")
    print(f"  rpc       {rpc or '(missing)'}")
    print(f"  chain_id  {chain or '(missing)'}")
    print(f"  pads      {pads}")
    print(f"  warp_cas  {curve[:12] + '…' if len(curve) > 12 else curve or '(missing)'}")
    print(f"  factory   {factory[:12] + '…' if len(factory) > 12 else factory or '(unset — ok for manual CA)'}")
    print(f"  wallet    {wallet[:10] + '…' if len(wallet) > 10 else wallet or '(missing)'}")
    print(f"  privkey   {'LOADED' if pk_set else 'MISSING'}")

    errors: list[str] = []
    if not rpc:
        errors.append("ARC_RPC_URL missing")
    if chain <= 0:
        errors.append("ARC_CHAIN_ID missing")
    if args.mainnet and "testnet" in rpc.lower():
        errors.append("mainnet mode but RPC still looks like testnet")
    if not curve.startswith("0x") or len(curve) < 42:
        errors.append("ARC_WARP_CAS / ARC_DEFAULT_CURVE missing (need a real curve)")
    if "tolly" in pads.lower() or "dyor" in pads.lower():
        if not (os.environ.get("ARC_TOLLY_CAS") or os.environ.get("ARC_DYOR_CAS")):
            print("  warn      tolly/dyor in ARC_PADS but no CA — prefer ARC_PADS=warp")

    block = None
    for url in (rpc, backup):
        if not url:
            continue
        try:
            block = int(_rpc(url, "eth_blockNumber", []), 16)  # type: ignore[arg-type]
            print(f"  block     {block}  via {url}")
            break
        except Exception as exc:  # noqa: BLE001
            print(f"  rpc_fail  {url} · {exc}")
    if block is None:
        errors.append("RPC unreachable")

    if curve.startswith("0x") and len(curve) >= 42 and rpc:
        try:
            code = _rpc(rpc, "eth_getCode", [curve, "latest"])
            if not code or code in ("0x", "0x0"):
                errors.append(f"curve {curve[:10]}… has no bytecode on this RPC")
            else:
                print(f"  curve     contract ok ({len(str(code)) // 2 - 1} bytes)")
                # buy / sell selectors present?
                blob = str(code).lower()
                for name, sel in (("buy", "d96a094a"), ("sell", "d79875eb")):
                    print(f"  selector  {name} 0x{sel} {'IN' if sel in blob else 'MISSING'}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"eth_getCode failed: {exc}")

    if wallet.startswith("0x") and rpc:
        try:
            from desk_realtime.arc_net import fetch_wallet_usdc

            info = fetch_wallet_usdc(wallet, force=True)
            print(f"  balance   raw={info.get('raw_usdc')} display={info.get('display_usdc')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  balance   probe skipped · {exc}")

    if errors:
        print("── FAIL ──")
        for e in errors:
            print(f"  • {e}")
        return 1
    print("── PASS ──")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
