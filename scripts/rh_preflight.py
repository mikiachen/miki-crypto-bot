#!/usr/bin/env python3
"""Robinhood Chain (4663) preflight — RPC, Uniswap v3 addresses, ETH balance.

Usage:
  python3 scripts/rh_preflight.py

Exits 0 on pass, 1 on fail. Never prints private keys.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k, v = k.strip(), v.strip().strip("'").strip('"')
        if k and k not in os.environ:
            os.environ[k] = v


def main() -> int:
    _load_dotenv(ROOT / ".env")
    _load_dotenv(ROOT / ".env.rh")
    sys.path[:0] = [str(ROOT / "grok-trading-desk" / ".vendor"), str(ROOT)]

    from desk_realtime.rh_net import (
        EXPLORER,
        USDG,
        WETH,
        chain_id,
        preflight,
        private_key,
        rpc_json,
        rpc_url,
        wallet,
    )
    from desk_realtime.rh_uniswap import FACTORY, QUOTER, SWAP_ROUTER, UNIVERSAL_ROUTER

    pf = preflight()
    pk_set = bool(private_key())
    who = wallet()

    print("══ Robinhood Chain preflight ══")
    print(f"  chain_id  {pf.get('chain_id')}  (expected {chain_id()})")
    print(f"  rpc       {rpc_url()}")
    from desk_realtime.rh_net import rpc_endpoints, tatum_api_key

    eps = rpc_endpoints()
    print(f"  rpc_pool  {len(eps)} endpoint(s)")
    for i, u in enumerate(eps):
        tag = "primary" if i == 0 else "backup"
        if "tatum" in u:
            tag += " · tatum" + (" key" if tatum_api_key() else " NO_KEY")
        print(f"            [{tag}] {u}")
    print(f"  explorer  {EXPLORER}")
    print(f"  block     {pf.get('block')}")
    print(f"  wallet    {(who[:10] + '…') if len(who) > 10 else who or '(missing)'}")
    print(f"  privkey   {'LOADED' if pk_set else 'MISSING'}")
    print(f"  eth       {float(pf.get('eth') or 0):.6f}")
    print(f"  WETH      {WETH}")
    print(f"  USDG      {USDG}")
    print(f"  factory   {FACTORY}")
    print(f"  quoter    {QUOTER}")
    print(f"  router02  {SWAP_ROUTER}")
    print(f"  ur        {UNIVERSAL_ROUTER}")

    errors: list[str] = []
    if not pf.get("ok"):
        errors.append(str(pf.get("reason") or "rpc preflight failed"))
    if int(pf.get("chain_id") or 0) != int(chain_id()):
        errors.append(f"chainId mismatch {pf.get('chain_id')}")

    for label, addr in (
        ("factory", FACTORY),
        ("quoter", QUOTER),
        ("router02", SWAP_ROUTER),
        ("WETH", WETH),
        ("USDG", USDG),
    ):
        try:
            code = rpc_json("eth_getCode", [addr, "latest"])
            if not code or code in ("0x", "0x0"):
                errors.append(f"{label} {addr[:10]}… has no bytecode")
            else:
                print(f"  code      {label} ok ({(len(str(code)) // 2) - 1} bytes)")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label} eth_getCode: {exc}")

    if who.startswith("0x") and len(who) == 42:
        try:
            from desk_realtime.rh_uniswap import erc20_balance, erc20_decimals

            dec = erc20_decimals(USDG)
            bal = erc20_balance(USDG, who)
            print(f"  usdg_dec  {dec}")
            print(f"  usdg_bal  {bal / (10 ** max(dec, 1)):.6f}")
        except Exception as exc:  # noqa: BLE001
            print(f"  usdg      probe skipped · {exc}")

    try:
        from desk_realtime.rh_strategy import liquid_board

        board = liquid_board(8)
        print(f"  board     {len(board)} uni-v3 row(s)")
        for row in board[:4]:
            print(
                f"            {row.get('symbol')} "
                f"m5={float(row.get('chg_m5') or 0):+.2f}% "
                f"m15={float(row.get('chg_m15') or 0):+.2f}% "
                f"liq={float(row.get('liquidity_usdg') or 0):.0f}"
            )
    except Exception as exc:  # noqa: BLE001
        print(f"  board     unread · {exc}")

    try:
        from desk_realtime.rhc_intel import api_key as rhc_key, refresh as rhc_refresh

        st = rhc_refresh(force=True)
        print(f"  rhc_key   {'LOADED' if rhc_key() else 'MISSING'}")
        print(
            f"  rhc       ok={st.get('ok')} hot={len(st.get('hot') or [])} "
            f"sm={len(st.get('smart_money') or [])} tape={len(st.get('trades') or [])}"
        )
        if not st.get("ok"):
            print(f"            {st.get('reason')}")
    except Exception as exc:  # noqa: BLE001
        print(f"  rhc       unread · {exc}")

    if errors:
        print("── FAIL ──")
        for e in errors:
            print(f"  • {e}")
        return 1
    print("── PASS ──")
    print(json.dumps({"ok": True, "chain_id": pf.get("chain_id"), "eth": pf.get("eth")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
