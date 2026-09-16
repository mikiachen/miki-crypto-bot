"""Arc hunt targets + single-bet sizing (faucet USDC risk control).

Universe now routes through Warp / Tolly / DYOR Fun launchpads.
"""

from __future__ import annotations

import os
from typing import Any

from desk_realtime.arc_launchpads import hunt_tuples, launchpad_universe

# Back-compat list — rebuilt from launchpad router
HUNT_TOKENS: list[tuple[str, str]] = hunt_tuples()

# Micro live size. Hard ceiling 1 USDC so ~14 USDC can fund 10+ fills.
# 0.5 is a valid trial; nothing above ARC_BET_HARD_CAP is sent.
BET_PCT = float(os.environ.get("ARC_BET_PCT", "1"))
BET_HARD_CAP = float(os.environ.get("ARC_BET_HARD_CAP", "1"))


def hunt_pool() -> list[tuple[str, str]]:
    return hunt_tuples()


def is_hunt_address(address: str) -> bool:
    a = (address or "").strip().lower()
    return any(a == t.address.lower() for t in launchpad_universe())


def _buy_ceiling() -> float:
    """Absolute live ceiling. Env cannot raise this above 1 USDC."""
    try:
        env_cap = float(BET_HARD_CAP)
    except (TypeError, ValueError):
        env_cap = 1.0
    if env_cap <= 0:
        return 1.0
    return min(env_cap, 1.0)


def capped_entry_usdc(balance_usdc: float, requested: float | None = None) -> float:
    """
    Micro fill for a ~14 USDC wallet.
    Default 0.5 USDC. Explicit 1.0 is allowed. Anything above 1 is cut to 1.
    0.5 is a legal trial size — it is not rounded up to the cap.
    """
    bal = max(0.0, float(balance_usdc or 0.0))
    ceiling = _buy_ceiling()
    gas_keep = 0.05
    if bal < 0.5 + gas_keep:
        return 0.0
    if requested is None:
        requested = min(0.5, ceiling)
    size = min(max(0.0, float(requested)), ceiling, bal - gas_keep)
    return round(size, 4)


def size_from_wallet(*, requested: float | None = None) -> dict[str, Any]:
    """Read live Arc USDC balance and return capped stake."""
    from desk_realtime.arc_net import fetch_wallet_usdc

    info = fetch_wallet_usdc(force=False)
    raw = float(info.get("raw_usdc") or 0.0)
    # Prefer on-chain; if still 0, do not invent 1000 — stake stays 0
    entry = capped_entry_usdc(raw, requested)
    return {
        "balance_usdc": raw,
        "display_usdc": float(info.get("display_usdc") or raw),
        "faked": bool(info.get("faked")),
        "entry_usdc": entry,
        "bet_pct": BET_PCT,
        "hard_cap": BET_HARD_CAP,
        "wallet": info.get("address"),
    }
