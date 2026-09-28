"""Desk quote-asset contract — Arc USDC / Robinhood Chain USDG / Solana SOL.

Arc (Circle L1): USDC is gas + settlement.
Robinhood Chain (4663): ETH gas; desk notional in USDG on Uniswap v3.
"""

from __future__ import annotations

import os

DESK_CHAIN = os.environ.get("DESK_CHAIN", "arc").strip().lower()

if DESK_CHAIN in ("robinhood", "rh", "rhchain"):
    QUOTE = "USDG"
    STAKE = float(os.environ.get("DESK_STAKE", os.environ.get("RH_BET_HARD_CAP", "0.3")))
    ENTRY = float(os.environ.get("DESK_ENTRY", os.environ.get("RH_BET_HARD_CAP", "0.3")))
    CHAIN_LABEL = "robinhood"
    CHAIN_ID = int(os.environ.get("RH_CHAIN_ID", "4663"))
    FEE_BLURB = "gas (ETH) and slippage included"
elif DESK_CHAIN == "arc":
    QUOTE = "USDC"
    # Principal follows live wallet when possible; env is fallback only
    STAKE = float(os.environ.get("DESK_STAKE", "0"))
    ENTRY = float(os.environ.get("DESK_ENTRY", "0.50"))
    CHAIN_LABEL = "arc"
    CHAIN_ID = int(os.environ.get("ARC_CHAIN_ID", "5042002"))
    FEE_BLURB = "gas (native USDC) and slippage included"
else:
    QUOTE = "SOL"
    STAKE = float(os.environ.get("DESK_STAKE", "2.50"))
    ENTRY = float(os.environ.get("DESK_ENTRY", "0.50"))
    CHAIN_LABEL = "solana"
    CHAIN_ID = 0
    FEE_BLURB = "fees, jito tips and slippage included"

# Back-compat aliases used across older call sites
ENTRY_SOL = ENTRY
STAKE_SOL = STAKE


def fmt_quote(amount: float, digits: int = 2) -> str:
    """Plain numeric amount (no FX conversion)."""
    return f"{float(amount):,.{digits}f}"


def fmt_quote_html(amount: float, digits: int = 2) -> str:
    return f'{fmt_quote(amount, digits)} <span class="unit">{QUOTE}</span>'


def fmt_with_unit(amount: float, digits: int = 2) -> str:
    return f"{fmt_quote(amount, digits)} {QUOTE}"


def normalize_fill_amount(raw: float | None) -> float:
    """Clamp / migrate legacy fill sizes into the active quote book."""
    amt = float(raw if raw is not None else ENTRY)
    if DESK_CHAIN == "arc":
        # Old Solana paper fills were ~0.50 SOL; remap into Arc ENTRY
        if 0.05 <= amt <= 1.0 and abs(amt - 0.5) < 0.15:
            return ENTRY
        # Legacy USD-sized paper (~75) → ENTRY
        if amt > STAKE * 2:
            return ENTRY
    elif DESK_CHAIN in ("robinhood", "rh", "rhchain"):
        if amt <= 0 or amt > max(STAKE * 5, 5.0):
            return ENTRY
    else:
        if amt > 20:
            return ENTRY
    return amt
