#!/usr/bin/env python3
"""Quote-only Uniswap v3 round-trip on Robinhood Chain. No broadcast by default.

Usage:
  python3 scripts/smoke_rh_uniswap_quote.py
  python3 scripts/smoke_rh_uniswap_quote.py --token 0x0339… --usdg 0.3
  RH_UNI_SEND=1 python3 scripts/smoke_rh_uniswap_quote.py --live --usdg 0.3

Default is QuoterV2 round-trip only. --live still requires RH_UNI_SEND=1.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_env() -> None:
    for name in (".env", ".env.rh"):
        path = ROOT / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


def main() -> int:
    _load_env()
    from desk_realtime.rh_net import USDG, WETH, preflight
    from desk_realtime.rh_strategy import liquid_board
    from desk_realtime.rh_uniswap import (
        execute_buy,
        execute_sell,
        hard_cap,
        plan_swap,
        quote_roundtrip,
        send_armed,
    )

    p = argparse.ArgumentParser(description="RH Chain Uniswap quote smoke")
    p.add_argument(
        "--token",
        default="",
        help="token address (default: WETH — most liquid USDG pair)",
    )
    p.add_argument("--usdg", type=float, default=0.0, help="USDG size (default hard cap)")
    p.add_argument(
        "--live",
        action="store_true",
        help="broadcast only when RH_UNI_SEND=1 is also set",
    )
    args = p.parse_args()

    pf = preflight()
    print("preflight", pf.get("ok"), "chain", pf.get("chain_id"), "eth", pf.get("eth"))
    if not pf.get("ok"):
        print("reason", pf.get("reason"))
        return 1

    token = (args.token or "").strip()
    symbol = "WETH"
    if not token:
        # Prefer WETH/USDG — the liquid major. Optional --token for board names.
        token = WETH
        board = liquid_board(5)
        for row in board:
            if str(row.get("address") or "").lower() == WETH.lower():
                symbol = str(row.get("symbol") or "WETH")
                break
    else:
        symbol = "?"
        for row in liquid_board(12):
            if str(row.get("address") or "").lower() == token.lower():
                symbol = str(row.get("symbol") or "?")
                break

    size = float(args.usdg) if args.usdg > 0 else hard_cap()
    q = quote_roundtrip(token, usdg_in=size)
    print("symbol", symbol)
    print("token", token[:10] + "…" + token[-4:] if len(token) == 42 else token)
    print("ok", q.get("ok"), "send_armed", send_armed(), "live_flag", bool(args.live))
    print("reason", q.get("reason"))
    print("usdg_in", q.get("usdg_in"), "usdg_out", q.get("usdg_out"), "fee", q.get("fee"))

    if not q.get("ok"):
        return 1

    if not args.live:
        # Optional Trading API plan probe (no broadcast).
        try:
            amount = str(int(round(float(q.get("usdg_in") or size) * 1_000_000)))
            plan = plan_swap(token_in=USDG, token_out=token, amount_in=amount)
            print("plan_ok", plan.get("ok"), "plan_reason", plan.get("reason"))
        except Exception as exc:  # noqa: BLE001
            print("plan_ok", False, "plan_reason", str(exc)[:120])
        print("mode quote-only · no broadcast")
        return 0

    if not send_armed():
        print("refused · set RH_UNI_SEND=1 for broadcast")
        return 2

    buy = execute_buy(token, usdg_in=size)
    print("buy_ok", buy.get("ok"), "tx", str(buy.get("tx_id") or "")[:18], buy.get("error") or "")
    if not buy.get("ok"):
        return 1
    sell = execute_sell(token)
    print("sell_ok", sell.get("ok"), "tx", str(sell.get("tx_id") or "")[:18], sell.get("error") or "")
    return 0 if sell.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
