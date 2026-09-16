"""Arc launchpad router — Warp / Tolly / DYOR Fun CA scan universe.

Env:
  ARC_PADS=warp            # comma list; default warp-only (no placeholder pads)
  ARC_WARP_CAS=0x...,0x...
  ARC_TOLLY_CAS=0x...,0x...   # only used if tolly ∈ ARC_PADS
  ARC_DYOR_CAS=0x...,0x...    # only used if dyor ∈ ARC_PADS
  ARC_ALLOW_PLACEHOLDERS=0    # never invent demo CAs when unset/0
"""

from __future__ import annotations

import os
import random
import threading
from dataclasses import asdict, dataclass
from typing import Any

# Tolly permanently locks 1% of USDC on buy — bake into EDGE + slippage
TOLLY_FEE_BPS = int(os.environ.get("ARC_TOLLY_FEE_BPS", "100"))  # 1%
# Extra cushion so cast send minOut is not rejected after fee
TOLLY_SLIP_BUFFER_BPS = int(os.environ.get("ARC_TOLLY_SLIP_BUFFER_BPS", "50"))

_LOCK = threading.RLock()


@dataclass(frozen=True)
class LaunchpadToken:
    symbol: str
    address: str
    launchpad: str  # warp | tolly | dyor
    fee_bps: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _split_cas(raw: str) -> list[str]:
    out: list[str] = []
    for part in (raw or "").replace(";", ",").split(","):
        a = part.strip()
        if a.startswith("0x") and len(a) >= 42:
            out.append(a)
    return out


def enabled_pads() -> list[str]:
    raw = (os.environ.get("ARC_PADS") or "warp").strip().lower()
    pads = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    allowed = {"warp", "tolly", "dyor"}
    out = [p for p in pads if p in allowed]
    return out or ["warp"]


def _allow_placeholders() -> bool:
    return os.environ.get("ARC_ALLOW_PLACEHOLDERS", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _pad(symbol: str, address: str, launchpad: str, fee_bps: int = 0) -> LaunchpadToken:
    return LaunchpadToken(
        symbol=symbol.upper(),
        address=address,
        launchpad=launchpad,
        fee_bps=fee_bps,
        note="tolly 1% lock" if launchpad == "tolly" else "",
    )


def _default_universe() -> list[LaunchpadToken]:
    """Build hunt list from env. Default: Warp only, no fake Tolly/DYOR CAs."""
    pads = set(enabled_pads())
    allow_ph = _allow_placeholders()
    tokens: list[LaunchpadToken] = []

    if "warp" in pads:
        warp = _split_cas(os.environ.get("ARC_WARP_CAS", ""))
        if not warp and allow_ph:
            warp = [
                os.environ.get("ARC_HUNT_A", "0xe2324ff2a59f8ecba8c321c6466e59121c00e775"),
            ]
        for i, a in enumerate(warp):
            tokens.append(_pad(f"WARP{chr(65 + (i % 26))}", a, "warp", 0))

    if "tolly" in pads:
        tolly = _split_cas(os.environ.get("ARC_TOLLY_CAS", ""))
        if not tolly and allow_ph:
            tolly = [
                os.environ.get("ARC_HUNT_B", "0x0c9535416fd3b772646c4575e0664fd65afeeeee"),
            ]
        for i, a in enumerate(tolly):
            tokens.append(_pad(f"TOLLY{chr(65 + (i % 26))}", a, "tolly", TOLLY_FEE_BPS))

    if "dyor" in pads:
        dyor = _split_cas(os.environ.get("ARC_DYOR_CAS", ""))
        if not dyor and allow_ph:
            dyor = [
                os.environ.get(
                    "ARC_DYOR_CA",
                    "0x1111111111111111111111111111111111111111",
                ),
            ]
        for i, a in enumerate(dyor):
            tokens.append(_pad(f"DYOR{chr(65 + (i % 26))}", a, "dyor", 0))

    return tokens


_UNIVERSE: list[LaunchpadToken] | None = None
_DYNAMIC: list[LaunchpadToken] = []


def launchpad_universe(*, refresh: bool = False) -> list[LaunchpadToken]:
    global _UNIVERSE
    with _LOCK:
        if _UNIVERSE is None or refresh:
            _UNIVERSE = _default_universe()
        by_addr: dict[str, LaunchpadToken] = {t.address.lower(): t for t in _UNIVERSE}
        for t in _DYNAMIC:
            by_addr[t.address.lower()] = t
        return list(by_addr.values())


def register_curve(
    address: str,
    *,
    launchpad: str = "warp",
    symbol: str = "",
    fee_bps: int | None = None,
) -> LaunchpadToken | None:
    """Hot-add a curve discovered by Factory watcher (thread-safe)."""
    a = (address or "").strip()
    if not (a.startswith("0x") and len(a) >= 42):
        return None
    pad = (launchpad or "warp").lower()
    if pad not in ("warp", "tolly", "dyor"):
        pad = "warp"
    fee = TOLLY_FEE_BPS if fee_bps is None and pad == "tolly" else int(fee_bps or 0)
    sym = (symbol or "").strip().upper().lstrip("$")
    if not sym:
        with _LOCK:
            n = len(_DYNAMIC)
        sym = f"{pad.upper()}{n % 26:X}"
    tok = _pad(sym, a, pad, fee)
    with _LOCK:
        _DYNAMIC[:] = [t for t in _DYNAMIC if t.address.lower() != a.lower()]
        _DYNAMIC.append(tok)
    return tok


def dynamic_curves() -> list[LaunchpadToken]:
    with _LOCK:
        return list(_DYNAMIC)


def pick_scan_target(rng: random.Random | None = None) -> LaunchpadToken:
    """Weighted HF scan across enabled pads (default Warp-only)."""
    rng = rng or random.Random()
    universe = launchpad_universe()
    if not universe:
        return _pad("EMPTY", "0x0000000000000000000000000000000000000000", "warp")
    by_pad: dict[str, list[LaunchpadToken]] = {}
    for t in universe:
        by_pad.setdefault(t.launchpad, []).append(t)
    pad = rng.choice(list(by_pad.keys()))
    return rng.choice(by_pad[pad])


def resolve_launchpad(address: str, symbol: str = "") -> LaunchpadToken | None:
    a = (address or "").strip().lower()
    sym = (symbol or "").strip().upper().lstrip("$")
    for t in launchpad_universe():
        if t.address.lower() == a:
            return t
        if sym and t.symbol == sym:
            return t
    if sym.startswith("TOLLY"):
        return _pad(sym or "TOLLY", address or "0x0", "tolly", TOLLY_FEE_BPS)
    if sym.startswith("DYOR"):
        return _pad(sym or "DYOR", address or "0x0", "dyor", 0)
    if sym.startswith("WARP"):
        return _pad(sym or "WARP", address or "0x0", "warp", 0)
    return None


def launchpad_fee_bps(address: str = "", symbol: str = "", launchpad: str = "") -> int:
    if (launchpad or "").lower() == "tolly":
        return TOLLY_FEE_BPS
    tok = resolve_launchpad(address, symbol)
    if tok:
        return int(tok.fee_bps)
    return 0


def edge_expectancy_adjust(
    raw_expectancy_r: float,
    *,
    launchpad: str = "",
    address: str = "",
    symbol: str = "",
) -> dict[str, Any]:
    """
    EDGE MODEL: subtract permanent pad fee from expectancy (in R units ≈ fee%).
    Tolly 1% lock → −0.01R on every round-trip buy.
    """
    fee_bps = launchpad_fee_bps(address, symbol, launchpad)
    fee_r = fee_bps / 10_000.0
    adjusted = float(raw_expectancy_r) - fee_r
    return {
        "raw_expectancy_r": round(float(raw_expectancy_r), 4),
        "fee_bps": fee_bps,
        "fee_r": round(fee_r, 4),
        "expectancy_r": round(adjusted, 4),
        "launchpad": (
            launchpad
            or (resolve_launchpad(address, symbol) or LaunchpadToken("", "", "warp")).launchpad
        ),
        "accepted": adjusted > 0,
    }


def hunt_tuples() -> list[tuple[str, str]]:
    """Back-compat (symbol, address) for CRYPTO_POOL consumers."""
    return [(t.symbol, t.address) for t in launchpad_universe()]
