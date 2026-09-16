"""Desk fast-forward / recording mode — 8h mission replayed in ~5 minutes.

Activate with:
  DESK_FASTFORWARD=1

Effects:
  * No live WebSocket / RPC — replay a deterministic tape of desk.jsonl events
  * Engine sleeps collapse to ~0.01s (see `pause` / `async_pause`)
  * UI should sleep ~0.05s between feed lines for smooth Streamlit refresh
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

_ROOT = Path(__file__).resolve().parents[1]
TAPE_PATH = _ROOT / "grok-trading-desk" / "logs" / "fastforward_tape.jsonl"
SESSION_SEC = 8 * 3600

from desk_realtime.desk_units import ENTRY as ENTRY_SOL, QUOTE, STAKE as STAKE_SOL

# Target wall-clock for full tape ≈ 5 minutes at ~0.25s effective Streamlit cycle
DEFAULT_EVENTS = 1100
UI_PAUSE_SEC = float(os.environ.get("DESK_FF_UI_PAUSE", "0.05"))
ENGINE_PAUSE_SEC = float(os.environ.get("DESK_FF_ENGINE_PAUSE", "0.01"))


def is_fastforward() -> bool:
    return os.environ.get("DESK_FASTFORWARD", "0").strip().lower() in (
        "1", "true", "yes", "on", "ff", "fast", "fastforward",
    )


def pause(seconds: float) -> None:
    """Replace time.sleep in accelerate mode."""
    if is_fastforward():
        time.sleep(ENGINE_PAUSE_SEC if seconds > 0 else 0)
    else:
        time.sleep(seconds)


async def async_pause(seconds: float) -> None:
    """Replace asyncio.sleep in accelerate mode."""
    if is_fastforward():
        await asyncio.sleep(ENGINE_PAUSE_SEC if seconds > 0 else 0)
    else:
        await asyncio.sleep(seconds)


def ui_frame_pause() -> None:
    """Streamlit: brief yield so the balance curve paints like 100× video."""
    time.sleep(UI_PAUSE_SEC)


def _iso(t0: datetime, elapsed: float) -> str:
    return (t0 + timedelta(seconds=float(elapsed))).isoformat()


def _token_pool(rng: random.Random, n: int = 100) -> list[str]:
    """100 meme tickers; ZZZ is the hero moonshot."""
    base = [
        "ZZZ", "ARCMEME", "USDCFUN", "CIRCLEX", "STABLEAI", "GASLESS", "ARCDOG", "EURCJET",
        "ARCFOX", "RUGLESS", "FROGAI", "CHADARC", "PUMPKIN", "DEGENX", "MOONDOG",
        "CATWIF", "JEETBOT", "ALPHA69", "NARWHAL", "GIGACHAD", "LOLCOIN", "YOLORUG",
        "BASED", "SIGMA", "MEWMEW", "POPCAT", "BRETT", "ANDY", "WOJAK", "PEPE2",
    ]
    out = list(base)
    while len(out) < n:
        stem = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(rng.randint(3, 6)))
        if stem not in out:
            out.append(stem)
    # Ensure ZZZ present
    if "ZZZ" not in out:
        out[0] = "ZZZ"
    return out[:n]


def generate_tape(
    path: Path | None = None,
    *,
    n_events: int = DEFAULT_EVENTS,
    n_tokens: int = 100,
    seed: int = 42,
    stake: float = STAKE_SOL,
    entry: float = ENTRY_SOL,
) -> Path:
    """Write a deterministic 8h virtual desk tape (jsonl). Idempotent overwrite."""
    path = path or TAPE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    tokens = _token_pool(rng, n_tokens)
    t0 = datetime(2026, 9, 13, 0, 0, 0, tzinfo=timezone.utc)

    events: list[dict[str, Any]] = []

    def push(elapsed: float, **rec: Any) -> None:
        row = dict(rec)
        row.setdefault("ts", _iso(t0, elapsed))
        row["sim_elapsed"] = round(float(elapsed), 3)
        events.append(row)

    # Boot
    push(
        0.0,
        type="action",
        symbol="DESK",
        action="ARM",
        reason=f"FASTFORWARD · principal {stake:.2f} {QUOTE} · 8h tape @ 100×",
        market="crypto",
        detail={"event": "BOOT", "stake": stake, "unit": QUOTE, "session_h": 8, "panel": "HOLD_OFF", "ff": True},
    )

    # Scripted ZZZ arc indices (moon → dump → bank)
    zzz_buy_i = int(n_events * 0.28)
    zzz_hold_marks = {int(n_events * x) for x in (0.34, 0.40, 0.46, 0.52)}
    zzz_exit_i = int(n_events * 0.58)

    open_sym: str | None = None
    open_entry = entry
    books = 0
    realized = 0.0

    for i in range(1, n_events):
        elapsed = (i / max(1, n_events - 1)) * SESSION_SEC
        tok = tokens[i % len(tokens)]

        # —— Hero: $ZZZ ——
        if i == zzz_buy_i:
            if open_sym is not None and open_sym != "ZZZ":
                # Flatten whatever is open so the moonshot can arm
                push(
                    elapsed,
                    type="close",
                    market="crypto",
                    symbol=open_sym,
                    pnl=round(open_entry * 0.3, 4),
                    hold_time=3.0,
                    detail={"event": "EXIT", "agent_type": "EXIT", "mult": 1.3,
                            "unit": QUOTE, "note": "rotate · clearing for $ZZZ", "ff": True},
                )
                open_sym = None
            if open_sym != "ZZZ":
                push(
                    elapsed,
                    type="action",
                    market="crypto",
                    symbol="ZZZ",
                    action="SCAN",
                    reason="anomaly · 100× volume spike",
                    detail={"bot": "scanner", "event": "SCAN", "agent_type": "SCANNER",
                            "note": "SCAN fresh launch $ZZZ · trench ignition"},
                )
                push(
                    elapsed + 0.5,
                    type="buy",
                    market="crypto",
                    symbol="ZZZ",
                    score=0.96,
                    amount=entry,
                    tx_id="ff_zzz_buy",
                    all_agent_scores={
                        "narrative": {"virality": 0.97},
                        "auditor": {"organic_score": 0.88},
                        "crypto_pulse": {"go_signal": 0.93},
                    },
                    detail={"event": "ENTRY", "agent_type": "TIMING",
                            "note": "liquidity doubled, veto gone · $ZZZ armed", "unit": QUOTE, "ff": True},
                )
                open_sym = "ZZZ"
                open_entry = entry
            continue

        if open_sym == "ZZZ" and i in zzz_hold_marks:
            mult = {int(n_events * 0.34): 4.2, int(n_events * 0.40): 11.0,
                    int(n_events * 0.46): 22.5, int(n_events * 0.52): 38.0}.get(i, 8.0)
            push(
                elapsed,
                type="action",
                market="crypto",
                symbol="ZZZ",
                action="WATCH",
                reason=f"$ZZZ mark {mult:.1f}x · trailing",
                detail={"event": "WATCH", "bot": "exit_manager", "agent_type": "EXIT",
                        "note": f"watching · {mult:.0f}x open", "mult": mult},
            )
            continue

        if i == zzz_exit_i:
            if open_sym == "ZZZ":
                mult = 31.0
                pnl = round(open_entry * (mult - 1.0), 4)
                realized += pnl
                books += 1
                push(
                    elapsed,
                    type="close",
                    market="crypto",
                    symbol="ZZZ",
                    pnl=pnl,
                    hold_time=420.0,
                    detail={"event": "EXIT", "agent_type": "EXIT", "mult": mult,
                            "unit": QUOTE, "note": "exit fired · 31x locked · $ZZZ banked", "ff": True},
                )
                open_sym = None
            continue

        # Block random ZZZ noise — hero is scripted only
        if tok == "ZZZ":
            tok = tokens[(i * 7) % len(tokens)]
            if tok == "ZZZ":
                tok = "PEPEJET"

        # While $ZZZ is open, only scripted marks/exit may touch the book
        if open_sym == "ZZZ":
            push(
                elapsed,
                type="action",
                market="crypto",
                symbol=tok,
                action="SCAN",
                reason="scan while $ZZZ riding",
                detail={"bot": "scanner", "event": "SCAN", "agent_type": "SCANNER",
                        "note": f"SCAN ${tok} · desk focused on $ZZZ"},
            )
            continue

        roll = rng.random()
        # Bias toward more action mid/late session for denser feed
        dens = 0.08 * math_sin_progress(i / n_events)

        if open_sym is None and roll < 0.14 + dens:
            # Entry (skip ZZZ except scripted)
            cand = tok if tok != "ZZZ" else rng.choice([t for t in tokens if t != "ZZZ"])
            score = round(rng.uniform(0.68, 0.94), 2)
            push(
                elapsed,
                type="action",
                market="crypto",
                symbol=cand,
                action="SCAN",
                reason="fresh launch spotted",
                detail={"bot": "scanner", "event": "SCAN", "agent_type": "SCANNER",
                        "note": f"SCAN fresh launch ${cand}"},
            )
            push(
                elapsed + 0.2,
                type="buy",
                market="crypto",
                symbol=cand,
                score=score,
                amount=entry,
                tx_id=f"ff_buy_{i}",
                all_agent_scores={
                    "narrative": {"virality": round(rng.uniform(0.65, 0.95), 2)},
                    "auditor": {"organic_score": round(rng.uniform(0.55, 0.9), 2)},
                    "crypto_pulse": {"go_signal": round(rng.uniform(0.5, 0.92), 2)},
                },
                detail={"event": "ENTRY", "agent_type": "TIMING",
                        "note": "liquidity doubled, veto gone", "unit": QUOTE, "ff": True},
            )
            open_sym = cand
            open_entry = entry
        elif open_sym is not None and roll < 0.22 + dens:
            # Mark / hold chatter
            mult = round(rng.uniform(0.85, 6.5), 2)
            # Violent swings for non-ZZZ
            if open_sym != "ZZZ" and rng.random() < 0.25:
                mult = round(rng.choice([0.4, 0.55, 8.0, 12.0, 0.3, 15.0]), 2)
            push(
                elapsed,
                type="action",
                market="crypto",
                symbol=open_sym,
                action="HOLD",
                reason="exit_manager",
                detail={"event": "HOLD", "agent_type": "EXIT", "bot": "exit_manager",
                        "note": f"watching · {mult:.0f}x open", "mult": mult},
            )
        elif open_sym is not None and roll < 0.34 + dens:
            # Exit — designed equity drift upward overall
            # Win rate ~62%, winners larger
            win = rng.random() < 0.62
            if win:
                mult = round(rng.uniform(1.4, 9.5), 2)
            else:
                mult = round(rng.uniform(0.25, 0.92), 2)
            pnl = round(open_entry * (mult - 1.0), 4)
            # Late session: slightly fatter winners so curve finishes strong
            if i > n_events * 0.7 and win:
                mult = round(rng.uniform(2.5, 14.0), 2)
                pnl = round(open_entry * (mult - 1.0), 4)
            realized += pnl
            books += 1
            push(
                elapsed,
                type="close",
                market="crypto",
                symbol=open_sym,
                pnl=pnl,
                hold_time=round(rng.uniform(0.4, 40), 2),
                detail={"event": "EXIT", "agent_type": "EXIT", "mult": mult,
                        "unit": QUOTE, "ff": True},
            )
            open_sym = None
        elif roll < 0.55:
            push(
                elapsed,
                type="skip",
                market="crypto",
                symbol=tok,
                reason="thin_liquidity",
                detail={"bot": "crypto_checker", "event": "NOT BUY", "agent_type": "RISK",
                        "note": "book too thin (risk veto)", "ff": True},
            )
        elif roll < 0.72:
            push(
                elapsed,
                type="skip",
                market="crypto",
                symbol=tok,
                reason="off_narrative",
                detail={"bot": "narrative", "event": "NOT BUY", "agent_type": "NARRATIVE",
                        "note": "off-narrative", "ff": True},
            )
        elif roll < 0.86:
            push(
                elapsed,
                type="action",
                market="crypto",
                symbol=tok,
                action="SCAN",
                reason="fresh launch spotted",
                detail={"bot": "scanner", "event": "SCAN", "agent_type": "SCANNER",
                        "note": f"SCAN fresh launch ${tok}"},
            )
        else:
            push(
                elapsed,
                type="action",
                market="crypto",
                symbol=tok,
                action="WATCH",
                reason="linked wallets queue sells",
                detail={"event": "WATCH", "bot": "auditor", "agent_type": "TIMING", "ff": True},
            )

    # Flatten any leftover book with a modest close so UI ends flat
    if open_sym is not None:
        push(
            SESSION_SEC - 30,
            type="close",
            market="crypto",
            symbol=open_sym,
            pnl=round(open_entry * 0.8, 4),
            hold_time=12.0,
            detail={"event": "EXIT", "agent_type": "EXIT", "mult": 1.8, "unit": QUOTE, "ff": True},
        )

    push(
        SESSION_SEC,
        type="action",
        symbol="DESK",
        action="DONE",
        reason=f"tape complete · books {books} · realized {realized:+.2f} {QUOTE}",
        market="crypto",
        detail={"event": "BOOT", "panel": "HOLD_OFF", "ff": True, "done": True},
    )

    with path.open("w", encoding="utf-8") as f:
        for row in events:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def math_sin_progress(x: float) -> float:
    import math
    return 0.5 + 0.5 * math.sin(x * math.pi)


def load_tape(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or TAPE_PATH
    if not path.is_file() or path.stat().st_size < 32:
        generate_tape(path)
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def ensure_tape(path: Path | None = None, *, force: bool = False) -> Path:
    path = path or TAPE_PATH
    if force or not path.is_file() or path.stat().st_size < 32:
        return generate_tape(path)
    return path


class TapePlayer:
    """Cursor over the fast-forward tape for Streamlit ticks."""

    def __init__(self, events: Iterable[dict[str, Any]] | None = None):
        self.events = list(events) if events is not None else load_tape()
        self.index = 0

    @property
    def done(self) -> bool:
        return self.index >= len(self.events)

    @property
    def total(self) -> int:
        return len(self.events)

    def peek_elapsed(self) -> float:
        if self.done:
            return float(SESSION_SEC)
        return float(self.events[self.index].get("sim_elapsed") or 0)

    def next_event(self) -> dict[str, Any] | None:
        if self.done:
            return None
        ev = self.events[self.index]
        self.index += 1
        return ev

    def take(self, n: int = 1) -> list[dict[str, Any]]:
        out = []
        for _ in range(max(1, n)):
            ev = self.next_event()
            if ev is None:
                break
            out.append(ev)
        return out


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Generate desk fast-forward tape")
    p.add_argument("--out", default=str(TAPE_PATH))
    p.add_argument("--events", type=int, default=DEFAULT_EVENTS)
    p.add_argument("--tokens", type=int, default=100)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--force", action="store_true")
    args = p.parse_args()
    out = Path(args.out)
    if args.force or not out.exists():
        path = generate_tape(out, n_events=args.events, n_tokens=args.tokens, seed=args.seed)
    else:
        path = out
    n = sum(1 for _ in path.open())
    print(f"tape → {path} ({n} events, ~{SESSION_SEC/3600:.0f}h sim)")


if __name__ == "__main__":
    main()
