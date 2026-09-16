#!/usr/bin/env python3
"""Smoke: buy → read tokens → sell on Warp curve (default DRY RUN).

  # dry (no broadcast)
  python3 scripts/smoke_arc_buy_sell.py

  # live tiny fill on CURRENT .env network (testnet recommended)
  python3 scripts/smoke_arc_buy_sell.py --live --usdc 0.5

Never prints private keys. Uses ARC_WARP_CAS / ARC_DEFAULT_CURVE.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
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


async def _run(*, live: bool, usdc: float, curve: str) -> int:
    os.environ["ARC_PADS"] = os.environ.get("ARC_PADS") or "warp"
    if live:
        os.environ["ARC_LIVE"] = "1"
        os.environ["ARC_FORCE_BROADCAST"] = "1"
    else:
        os.environ["ARC_LIVE"] = "0"
        os.environ["ARC_FORCE_BROADCAST"] = "0"

    from desk_realtime.arc_executor import ArcExecutor
    from desk_realtime.foundry_bin import get_chain_id, get_explorer, get_rpc_url

    ex = ArcExecutor({"live": live, "default_curve": curve})
    print(f"rpc    {get_rpc_url()}")
    print(f"chain  {get_chain_id()}")
    print(f"curve  {curve}")
    print(f"live   {live}  stake={usdc} USDC")

    buy = await ex.buy(curve, usdc, symbol="SMOKE", launchpad="warp")
    print(
        "BUY ",
        {
            "ok": buy.get("ok"),
            "dry_run": buy.get("dry_run"),
            "tx_id": (buy.get("tx_id") or "")[:18],
            "token_amount": buy.get("token_amount"),
            "meme_token": (buy.get("meme_token") or "")[:12],
            "error": buy.get("error") or "",
        },
    )
    if not buy.get("ok"):
        return 1

    amount = int(buy.get("token_amount") or 0)
    if live and amount <= 0:
        try:
            held = ex.tokens_held(curve)
            amount = int(held.get("token_amount") or 0)
            print("held ", held)
        except Exception as exc:  # noqa: BLE001
            print("held_fail", exc)

    sell = await ex.sell(
        curve,
        token_amount=amount,
        symbol="SMOKE",
        fraction=1.0,
        launchpad="warp",
        panic=False,
    )
    print(
        "SELL",
        {
            "ok": sell.get("ok"),
            "dry_run": sell.get("dry_run"),
            "tx_id": (sell.get("tx_id") or "")[:18],
            "token_amount": sell.get("token_amount"),
            "min_usdc_out": sell.get("min_usdc_out"),
            "error": sell.get("error") or "",
        },
    )
    explorer = get_explorer().rstrip("/")
    for label, fill in (("buy", buy), ("sell", sell)):
        tx = fill.get("tx_id") or ""
        if tx.startswith("0x") and len(tx) >= 66:
            print(f"  {label}_url {explorer}/tx/{tx}")
    return 0 if sell.get("ok") else 1


def main() -> int:
    _load_dotenv(ROOT / ".env")
    sys.path[:0] = [str(ROOT / "grok-trading-desk" / ".vendor"), str(ROOT)]

    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="Broadcast real txs (dangerous)")
    ap.add_argument("--usdc", type=float, default=0.5, help="Buy size in USDC")
    ap.add_argument("--curve", default="", help="Override curve CA")
    args = ap.parse_args()

    curve = (
        args.curve.strip()
        or (os.environ.get("ARC_DEFAULT_CURVE") or "").strip()
        or (os.environ.get("ARC_WARP_CAS") or "").split(",")[0].strip()
    )
    if not (curve.startswith("0x") and len(curve) >= 42):
        print("ERROR: set ARC_WARP_CAS / ARC_DEFAULT_CURVE or --curve")
        return 1
    if args.live and args.usdc > 5:
        print("ERROR: --live refuse stake > 5 USDC (raise only after proven)")
        return 1

    return asyncio.run(_run(live=args.live, usdc=float(args.usdc), curve=curve))


if __name__ == "__main__":
    raise SystemExit(main())
