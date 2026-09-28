#!/usr/bin/env python3
"""Dry-run Uniswap Trading API plan on Arc. Never prints keys.

Usage:
  python3 scripts/smoke_arc_uniswap_swap.py
  python3 scripts/smoke_arc_uniswap_swap.py --token 0xeCe5… --usdc 0.5
  ARC_UNI_SEND=1 python3 scripts/smoke_arc_uniswap_swap.py --live --usdc 0.5

Default is plan-only. --live still requires ARC_UNI_SEND=1.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_env() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
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
    p = argparse.ArgumentParser(description="Arc Uniswap swap plan smoke")
    p.add_argument(
        "--token",
        default="0xeCe5cA8bf9220718E5727754026757512212cb3c",
        help="token out (default ARGUS)",
    )
    p.add_argument("--usdc", type=float, default=0.5)
    p.add_argument("--symbol", default="ARGUS")
    p.add_argument(
        "--live",
        action="store_true",
        help="broadcast only when ARC_UNI_SEND=1 is also set",
    )
    args = p.parse_args()

    from desk_realtime.arc_uniswap import build_swap_plan, execute_swap_plan, send_armed

    plan = build_swap_plan(args.token, usdc_in=float(args.usdc), symbol=args.symbol)
    print("ok", plan.get("ok"), "send_armed", send_armed(), "live_flag", bool(args.live))
    print("reason", plan.get("reason"))
    print("routing", plan.get("routing"), "usdc_in", plan.get("usdc_in"))
    router = str(plan.get("router") or "")
    if len(router) == 42:
        print("router", router[:6] + "…" + router[-4:])
    swap = plan.get("swap_tx") if isinstance(plan.get("swap_tx"), dict) else {}
    if swap:
        to = str(swap.get("to") or "")
        print("swap_to", (to[:6] + "…" + to[-4:]) if len(to) == 42 else "none")
        print("calldata_bytes", max(0, (len(str(swap.get("data") or "")) - 2) // 2))
        print("has_approval", bool(plan.get("approval_tx")))
    if not args.live:
        print("mode plan-only · no broadcast")
        return 0 if plan.get("ok") else 1
    if not send_armed():
        print("refused · set ARC_UNI_SEND=1 for broadcast")
        return 2
    result = execute_swap_plan(plan)
    print("broadcast_ok", result.get("ok"), "tx", str(result.get("tx_id") or "")[:18])
    print("broadcast_reason", result.get("reason"))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
