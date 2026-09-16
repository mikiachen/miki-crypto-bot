"""LLM token-fee budget — paid OpenRouter stays on while the wallet has USDC.

Rules (Arc desk):
  1. Cut the paid API only when on-chain USDC is 0. A 100 USDC reserve does not apply.
  2. Each LLM call still records LLM_COST_PER_CALL_USDC on a local credit ledger
     (accounting only — not a chain transfer, and not a gate).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("llm_budget")

_ROOT = Path(__file__).resolve().parents[1]
_LEDGER = _ROOT / "grok-trading-desk" / "logs" / "llm_credit.json"
_LOCK = threading.Lock()

# Keep this much USDC for trading — API cannot eat into it
LLM_RESERVE_USDC = float(os.environ.get("LLM_RESERVE_USDC", "100"))
# Estimated OpenRouter cost per narrative call (USD ≈ USDC face)
LLM_COST_PER_CALL_USDC = float(os.environ.get("LLM_COST_PER_CALL_USDC", "0.03"))
# Require earned credit before LLM (1=on). Bots unlock by realizing PnL.
LLM_REQUIRE_EARNINGS = os.environ.get("LLM_REQUIRE_EARNINGS", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
# Fraction of positive realized PnL that funds LLM credit
LLM_EARN_SHARE = float(os.environ.get("LLM_EARN_SHARE", "0.10"))
# One-time bootstrap if surplus is healthy (USDC)
LLM_BOOTSTRAP_USDC = float(os.environ.get("LLM_BOOTSTRAP_USDC", "0.15"))


def _load() -> dict[str, Any]:
    if not _LEDGER.is_file():
        return {"credit": 0.0, "spent": 0.0, "earned": 0.0, "calls": 0, "bootstrap": False}
    try:
        raw = json.loads(_LEDGER.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {"credit": 0.0, "spent": 0.0, "earned": 0.0, "calls": 0, "bootstrap": False}


def _save(state: dict[str, Any]) -> None:
    _LEDGER.parent.mkdir(parents=True, exist_ok=True)
    tmp = _LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(_LEDGER)


def wallet_usdc() -> float:
    try:
        from desk_realtime.arc_net import fetch_wallet_usdc

        return float(fetch_wallet_usdc().get("raw_usdc") or 0.0)
    except Exception:
        return 0.0


def status() -> dict[str, Any]:
    with _LOCK:
        st = _load()
        bal = wallet_usdc()
        surplus = bal - LLM_RESERVE_USDC
        credit = float(st.get("credit") or 0.0)
        ok, reason = _decide(bal, credit, st)
        return {
            "allow": ok,
            "reason": reason,
            "balance_usdc": bal,
            "reserve_usdc": LLM_RESERVE_USDC,
            "surplus_usdc": surplus,
            "credit_usdc": credit,
            "cost_per_call": LLM_COST_PER_CALL_USDC,
            "earned_usdc": float(st.get("earned") or 0.0),
            "spent_usdc": float(st.get("spent") or 0.0),
            "calls": int(st.get("calls") or 0),
            "require_earnings": LLM_REQUIRE_EARNINGS,
        }


def _decide(bal: float, credit: float, st: dict[str, Any]) -> tuple[bool, str]:
    # Paid OpenRouter stays on until the wallet is empty. Earnings credit is
    # a ledger only — it must not cut the API while USDC remains.
    del credit, st
    if bal <= 0:
        return False, "钱包归零 · API切断"
    return True, "ok"


def allow_llm_call() -> tuple[bool, str]:
    """Gate before any OpenRouter/Anthropic request."""
    with _LOCK:
        st = _load()
        bal = wallet_usdc()
        credit = float(st.get("credit") or 0.0)
        ok, reason = _decide(bal, credit, st)
        if ok and reason == "bootstrap":
            st["credit"] = float(st.get("credit") or 0.0) + LLM_BOOTSTRAP_USDC
            st["bootstrap"] = True
            _save(st)
            log.info("LLM bootstrap credit +%.3f USDC", LLM_BOOTSTRAP_USDC)
            return True, "bootstrap_granted"
        return ok, reason


def charge_llm_call() -> None:
    with _LOCK:
        st = _load()
        cost = LLM_COST_PER_CALL_USDC
        st["credit"] = max(0.0, float(st.get("credit") or 0.0) - cost)
        st["spent"] = float(st.get("spent") or 0.0) + cost
        st["calls"] = int(st.get("calls") or 0) + 1
        st["ts"] = time.time()
        _save(st)


def credit_from_pnl(pnl_usdc: float) -> float:
    """Route a share of positive realized PnL into LLM token-fee credit."""
    if pnl_usdc <= 0:
        return 0.0
    add = float(pnl_usdc) * LLM_EARN_SHARE
    with _LOCK:
        st = _load()
        st["credit"] = float(st.get("credit") or 0.0) + add
        st["earned"] = float(st.get("earned") or 0.0) + add
        st["ts"] = time.time()
        _save(st)
    log.info("LLM credit +%.4f from pnl %.4f", add, pnl_usdc)
    return add
