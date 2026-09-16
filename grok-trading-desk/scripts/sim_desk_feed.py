#!/usr/bin/env python3
"""Paper SIM event feeder for the 12-bot desk UI.

When the live orchestrator is offline (or MODE=sim), this process appends
realistic JSONL records to logs/desk.jsonl so Streamlit can show a living desk.

    PYTHONPATH=.vendor:. python scripts/sim_desk_feed.py
"""

from __future__ import annotations

import argparse
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = ROOT / "logs" / "desk.jsonl"

CRYPTO = ["PEPEJET", "HOODAI", "TRENCH", "BONKAI", "MOGFUN", "WIFBOT", "SOLCAT"]
STOCKS = ["NVDA", "TSLA", "AAPL", "AMD", "PLTR", "SOFI", "COIN"]

BOTS = [
    "scout", "auditor", "narrative", "screener", "analyst", "radar", "insider",
    "crypto_pulse", "market_pulse", "allocator", "crypto_checker", "stock_checker",
    "exit_manager",
]


def write(path: Path, record_type: str, **fields) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": datetime.now(timezone.utc).isoformat(), "type": record_type, **fields}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def seed(path: Path) -> None:
    write(path, "allocation", crypto_pct=0.55, stocks_pct=0.45,
          reason="sim boot · meme season mild · stocks mixed")
    write(path, "cost", cost_usd=0.0, calls=0, fallbacks=0, by_agent={})
    write(path, "skip", market="crypto", symbol="RUGLESS", reason="audit_fail",
          detail={"bot": "auditor", "wash_trading": True})
    write(path, "skip", market="crypto", symbol="PEPEJET", reason="checker_veto",
          detail={"bot": "crypto_checker", "veto_reason": "sniper cluster"})
    write(path, "buy", market="crypto", symbol="HOODAI", score=0.71,
          amount=75.0, tx_id="dry_run",
          all_agent_scores={"auditor": {"organic_score": 0.8},
                            "narrative": {"virality": 0.74},
                            "crypto_pulse": {"go_signal": 0.62}})
    write(path, "skip", market="stocks", symbol="SOFI", reason="low_score",
          detail={"bot": "analyst", "score": 0.41})
    write(path, "action", symbol="HOODAI", action="HOLD",
          reason="thesis intact · exit_manager", market="crypto")


def tick(path: Path, n: int) -> None:
    rng = random.Random(time.time_ns() ^ n)
    tok = rng.choice(CRYPTO)
    stk = rng.choice(STOCKS)
    bot = rng.choice(BOTS)
    roll = rng.random()

    if roll < 0.35:
        write(path, "skip", market="crypto", symbol=tok,
              reason=rng.choice(["audit_fail", "low_score", "checker_veto", "pulse_cold"]),
              detail={"bot": bot, "sim": True})
    elif roll < 0.50:
        write(path, "skip", market="stocks", symbol=stk,
              reason=rng.choice(["controversy", "insider_dump", "low_score", "checker_veto"]),
              detail={"bot": bot, "sim": True})
    elif roll < 0.62:
        market = rng.choice(["crypto", "stocks"])
        sym = tok if market == "crypto" else stk
        write(path, "buy", market=market, symbol=sym, score=round(rng.uniform(0.62, 0.9), 2),
              amount=round(rng.uniform(40, 180), 2), tx_id="dry_run",
              all_agent_scores={"sim": True, "bot": bot})
    elif roll < 0.75:
        write(path, "action", symbol=rng.choice(CRYPTO + STOCKS),
              action=rng.choice(["HOLD", "TIGHTEN", "TRIM", "CLOSE"]),
              reason=f"{bot} review", market=rng.choice(["crypto", "stocks"]))
    elif roll < 0.85:
        write(path, "close", market=rng.choice(["crypto", "stocks"]),
              symbol=rng.choice(CRYPTO + STOCKS),
              pnl=round(rng.uniform(-40, 90), 2), hold_time=round(rng.uniform(0.2, 48), 2))
    elif roll < 0.93:
        crypto_pct = round(rng.uniform(0.2, 0.75), 2)
        write(path, "allocation", crypto_pct=crypto_pct, stocks_pct=round(1 - crypto_pct, 2),
              reason=f"allocator regime tick · {bot}")
    else:
        write(path, "cost", cost_usd=round(rng.uniform(0.01, 0.4), 4),
              calls=rng.randint(1, 12), fallbacks=rng.randint(0, 2),
              by_agent={bot: round(rng.uniform(0.01, 0.15), 4)})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default=str(DEFAULT_LOG))
    parser.add_argument("--interval", type=float, default=4.0)
    parser.add_argument("--seed-only", action="store_true")
    args = parser.parse_args()
    path = Path(args.log)

    if not path.exists() or path.stat().st_size == 0:
        seed(path)
        print(f"seeded {path}")
    if args.seed_only:
        return

    print(f"sim feed → {path} every {args.interval}s (CTRL+C to stop)")
    n = 0
    while True:
        n += 1
        tick(path, n)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
