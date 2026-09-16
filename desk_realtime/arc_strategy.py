"""Arc HF strategy math — USDC-native exits, consensus filters, anti-snipe gas, net PnL.

1. Dynamic slippage for panic / trailing stop → 15–20% (Arc USDC gas = no SOL-gas trap)
2. Agent consensus: liquidity depth · wallet cluster · narrative heat
3. Priority fee +10% on cast send (anti-snipe)
4. Realized net USDC after gas · valve tighten on drawdown
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from desk_realtime.foundry_bin import (
    ensure_foundry_on_path,
    get_chain_id,
    get_rpc_url,
    rpc_endpoints,
    tool_path,
)
from desk_realtime.secrets import sanitize_exc

log = logging.getLogger("arc_strategy")

_ROOT = Path(__file__).resolve().parents[1]
_PNL_LEDGER = _ROOT / "grok-trading-desk" / "logs" / "realized_pnl.json"
_LOCK = threading.Lock()

# —— 1. Slippage (bps) ——
SLIP_NORMAL_BPS = int(os.environ.get("ARC_SLIP_NORMAL_BPS", "500"))  # 5%
SLIP_PANIC_MIN_BPS = int(os.environ.get("ARC_SLIP_PANIC_MIN_BPS", "1500"))  # 15%
SLIP_PANIC_MAX_BPS = int(os.environ.get("ARC_SLIP_PANIC_MAX_BPS", "2000"))  # 20%
SLIP_TRAIL_MIN_BPS = int(os.environ.get("ARC_SLIP_TRAIL_MIN_BPS", "1500"))

# —— 2. Consensus thresholds ——
LIQ_MIN_USDC = float(os.environ.get("ARC_LIQ_MIN_USDC", "5000"))
CLUSTER_TOP_N = int(os.environ.get("ARC_CLUSTER_TOP_N", "10"))
CLUSTER_VETO_SCORE = float(os.environ.get("ARC_CLUSTER_VETO", "0.55"))

# —— 3. Anti-snipe ——
PRIORITY_BUMP = float(os.environ.get("ARC_PRIORITY_BUMP", "1.10"))  # +10%

# —— 4. PnL / valve ——
GAS_ESTIMATE_USDC = float(os.environ.get("ARC_GAS_ESTIMATE_USDC", "0.02"))
DRAWDOWN_VALVE_PCT = float(os.environ.get("ARC_DRAWDOWN_VALVE_PCT", "0.12"))  # 12% from peak unreal


@dataclass
class ConsensusResult:
    ok: bool
    veto: bool
    liquidity_usdc: float
    liq_veto: bool
    cluster_score: float
    cluster_veto: bool
    narrative_index: float
    warnings: list[str] = field(default_factory=list)
    feed_alert: str = ""  # red-bar text for DESK FEED
    source: str = "strategy"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def dynamic_slippage_bps(
    *,
    panic: bool = False,
    trailing: bool = False,
    urgency: float = 0.0,
    launchpad: str = "",
    address: str = "",
    symbol: str = "",
    platform_fee_bps: int | None = None,
) -> int:
    """
    Arc USDC-native: widen slippage on emergency exits so the tx lands in ~1 block.
    urgency in [0,1] interpolates between panic min/max.
    Tolly (+other pads): add permanent platform fee into minOut tolerance so cast
    send is not rejected on-chain.
    """
    if panic or trailing:
        u = max(0.0, min(1.0, float(urgency)))
        lo = SLIP_PANIC_MIN_BPS if panic else SLIP_TRAIL_MIN_BPS
        hi = SLIP_PANIC_MAX_BPS
        base = int(round(lo + (hi - lo) * u))
    else:
        base = SLIP_NORMAL_BPS

    fee = platform_fee_bps
    if fee is None:
        try:
            from desk_realtime.arc_launchpads import (
                TOLLY_SLIP_BUFFER_BPS,
                launchpad_fee_bps,
            )

            fee = launchpad_fee_bps(address, symbol, launchpad)
            if fee > 0:
                fee = int(fee) + int(TOLLY_SLIP_BUFFER_BPS)
        except Exception:
            fee = 0
    return min(9_900, int(base) + int(fee or 0))


def edge_model_r(
    win_rate: float,
    avg_win_r: float,
    avg_loss_r: float,
    *,
    launchpad: str = "",
    address: str = "",
    symbol: str = "",
) -> dict[str, Any]:
    """EDGE MODEL expectancy with launchpad fee drag (Tolly 1%)."""
    wr = max(0.0, min(1.0, float(win_rate)))
    raw = wr * float(avg_win_r) - (1.0 - wr) * abs(float(avg_loss_r))
    try:
        from desk_realtime.arc_launchpads import edge_expectancy_adjust

        return edge_expectancy_adjust(
            raw, launchpad=launchpad, address=address, symbol=symbol
        )
    except Exception:
        return {
            "raw_expectancy_r": round(raw, 4),
            "fee_bps": 0,
            "expectancy_r": round(raw, 4),
            "accepted": raw > 0,
        }


def min_tokens_out_from_slippage(expected_out: int, slippage_bps: int) -> int:
    """Floor tokens received after slippage tolerance."""
    if expected_out <= 0:
        return 0
    bps = max(0, min(9_900, int(slippage_bps)))
    return max(0, int(expected_out * (10_000 - bps) // 10_000))


def bumped_priority_gwei(base_gwei: float | None = None) -> str:
    """Anti-snipe: priority fee × 1.10 for cast --priority-gas-price."""
    base = float(base_gwei if base_gwei is not None else os.environ.get("ARC_BASE_PRIORITY_GWEI", "20"))
    bumped = base * PRIORITY_BUMP
    # cast accepts e.g. 22gwei
    if bumped >= 1:
        return f"{bumped:.4g}gwei"
    return f"{bumped}gwei"


def bumped_gas_price_gwei(base_gwei: float | None = None) -> str:
    base = float(base_gwei if base_gwei is not None else os.environ.get("ARC_BASE_GAS_GWEI", "20"))
    return f"{base * PRIORITY_BUMP:.4g}gwei"


def _cast_balance_usdc(address: str, timeout: float = 8.0) -> float | None:
    """Native USDC balance of an address via cast (pool / pair probe)."""
    ensure_foundry_on_path()
    cast = tool_path("cast")
    if cast is None:
        return None
    try:
        for rpc in rpc_endpoints():
            proc = subprocess.run(
                [str(cast), "balance", address, "--rpc-url", rpc],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if proc.returncode != 0:
                continue
            line = (proc.stdout or "").strip().splitlines()[-1]
            wei = int(line, 0)
            return wei / 1e18
        return None
    except Exception as exc:  # noqa: BLE001
        log.info("cast balance liq probe failed: %s", sanitize_exc(exc))
        return None


def probe_liquidity_usdc(token_or_pool: str) -> dict[str, Any]:
    """
    Liquidity depth in USDC. Prefer cast balance of pool/token contract
    (Arc native USDC). Falls back to deterministic stub for paper.
    """
    addr = (token_or_pool or "").strip()
    if addr.startswith("0x") and len(addr) >= 42:
        try:
            from desk_realtime.arc_dex import dex_enabled, liquidity_snapshot

            if dex_enabled():
                snap = liquidity_snapshot(addr)
                depth = float(snap.get("liquidity_usdc") or 0)
                mainnet = os.environ.get("ARC_NETWORK", "").strip().lower() == "mainnet"
                # A listed pair is not a buy. The $5000 floor is paper-AMM, not Warp.
                veto = (not mainnet) and ((not snap.get("indexed")) or depth < LIQ_MIN_USDC)
                return {
                    "liquidity_usdc": depth,
                    "source": "dexscreener" if snap.get("indexed") else "dexscreener-miss",
                    "veto": veto,
                    "threshold": LIQ_MIN_USDC,
                    "symbol": snap.get("symbol") or "",
                    "url": snap.get("url") or "",
                }
        except Exception as exc:  # noqa: BLE001
            log.info("dex liquidity skipped: %s", sanitize_exc(exc))
        live = _cast_balance_usdc(addr)
        if live is not None and os.environ.get("ARC_NETWORK", "").strip().lower() != "mainnet":
            return {
                "liquidity_usdc": live,
                "source": "cast",
                "veto": live < LIQ_MIN_USDC,
                "threshold": LIQ_MIN_USDC,
            }
    if os.environ.get("ARC_NETWORK", "").strip().lower() == "mainnet":
        # Bonding curves are not a $5000 AMM book. Depth is quoteBuy/quoteSell.
        # Dexscreener missing is not a buy, and not a fake veto.
        return {
            "liquidity_usdc": 0.0,
            "source": "on-chain quote",
            "veto": False,
            "threshold": LIQ_MIN_USDC,
        }
    # Stub: hash → 1k–80k range so thin pools still get vetoed often
    seed = int(hashlib.sha256(addr.lower().encode() or b"x").hexdigest()[:8], 16)
    stub = 800.0 + (seed % 90_000)
    # Bias hunt tokens toward deeper books for demo variety
    if stub < LIQ_MIN_USDC and (seed % 3 == 0):
        stub = LIQ_MIN_USDC + (seed % 20_000)
    return {
        "liquidity_usdc": round(stub, 2),
        "source": "stub",
        "veto": stub < LIQ_MIN_USDC,
        "threshold": LIQ_MIN_USDC,
    }


def detect_wallet_cluster(token: str, address: str) -> dict[str, Any]:
    """
    Top-sender share on recent transfers. Shared with narrative via cluster_book.
    No score (and no veto) when the chain has no transfer logs yet.
    """
    from desk_realtime.cluster_book import shared_cluster

    live = shared_cluster(token, address)
    score = live.get("cluster_score")
    if score is None:
        return {
            "cluster_score": 0.0,
            "top_n": CLUSTER_TOP_N,
            "veto": False,
            "source": live.get("source") or "empty",
            "note": "no shared transfer sample yet",
            "mentions": live.get("mentions"),
        }
    veto = float(score) >= CLUSTER_VETO_SCORE
    return {
        "cluster_score": float(score),
        "top_n": CLUSTER_TOP_N,
        "veto": veto,
        "source": live.get("source") or "shared",
        "note": "same-mother-wallet risk" if veto else "cluster clear",
        "mentions": live.get("mentions"),
    }


def narrative_heat_index(
    token: str,
    *,
    mention_count: int = 0,
    trade_count: int = 0,
    llm_score: float | None = None,
) -> float:
    """
    Narrative Index 0–100. Blend mention frequency, trade tempo, optional LLM score.
    """
    mentions = max(0, int(mention_count))
    trades = max(0, int(trade_count))
    # Soft saturating curves
    m_term = 100.0 * (1.0 - pow(2.718281828, -mentions / 8.0))
    t_term = 100.0 * (1.0 - pow(2.718281828, -trades / 20.0))
    heat = 0.45 * m_term + 0.35 * t_term
    if llm_score is not None:
        heat = 0.7 * heat + 0.3 * (float(llm_score) * 100.0)
    # Deterministic ambient if no telemetry yet
    if mentions == 0 and trades == 0 and llm_score is None:
        if os.environ.get("ARC_NETWORK", "").strip().lower() == "mainnet":
            heat = 0.0
        else:
            seed = int(hashlib.md5(token.upper().encode()).hexdigest()[:6], 16)
            heat = 35.0 + (seed % 55)
    return round(max(0.0, min(100.0, heat)), 1)


def run_consensus_filter(
    token: str,
    address: str,
    *,
    mention_count: int = 0,
    trade_count: int = 0,
    llm_score: float | None = None,
) -> ConsensusResult:
    """Three-axis gate before TIMING / buy."""
    liq = probe_liquidity_usdc(address)
    cluster = detect_wallet_cluster(token, address)
    narr = narrative_heat_index(
        token,
        mention_count=mention_count,
        trade_count=trade_count,
        llm_score=llm_score,
    )
    warnings: list[str] = []
    feed = ""
    liq_veto = bool(liq["veto"])
    cluster_veto = bool(cluster["veto"])

    if liq_veto:
        warnings.append(
            f"LIQ VETO · pool {liq['liquidity_usdc']:.0f} USDC < {LIQ_MIN_USDC:.0f}"
        )
    if cluster_veto:
        msg = (
            f"CLUSTER WARN · top{CLUSTER_TOP_N} linked "
            f"score={cluster['cluster_score']:.2f} · 老鼠仓嫌疑"
        )
        warnings.append(msg)
        feed = f"⚠ RED · {msg} · BUY CANCELLED"

    veto = liq_veto or cluster_veto
    # Soft narrative veto only if heat extremely cold AND no hard veto yet
    if not veto and narr < 18.0:
        warnings.append(f"NARRATIVE cold · index {narr:.0f}%")
        # do not hard-veto on narrative alone — RISK/NARRATIVE agents handle

    return ConsensusResult(
        ok=not veto,
        veto=veto,
        liquidity_usdc=float(liq["liquidity_usdc"]),
        liq_veto=liq_veto,
        cluster_score=float(cluster["cluster_score"]),
        cluster_veto=cluster_veto,
        narrative_index=narr,
        warnings=warnings,
        feed_alert=feed,
        source=str(liq.get("source") or "strategy"),
    )


# —— Realized PnL ledger (net of Arc gas) ——


def _pnl_load() -> dict[str, Any]:
    if not _PNL_LEDGER.is_file():
        return {
            "realized_net_usdc": 0.0,
            "realized_gross_usdc": 0.0,
            "gas_paid_usdc": 0.0,
            "peak_equity_usdc": 0.0,
            "closes": 0,
            "valve_tight": False,
        }
    try:
        return json.loads(_PNL_LEDGER.read_text(encoding="utf-8"))
    except Exception:
        return {"realized_net_usdc": 0.0, "closes": 0}


def _pnl_save(state: dict[str, Any]) -> None:
    _PNL_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PNL_LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(_PNL_LEDGER)


def record_close_pnl(
    *,
    entry_usdc: float,
    exit_mult: float,
    gas_usdc: float | None = None,
    equity_mark: float | None = None,
) -> dict[str, Any]:
    """
    Net USDC after Arc gas → REALIZED PnL.
    Also updates peak equity and may signal valve tighten on drawdown.
    """
    gas = float(gas_usdc if gas_usdc is not None else GAS_ESTIMATE_USDC)
    gross = float(entry_usdc) * (float(exit_mult) - 1.0)
    # Round-trip gas (buy+sell) on USDC-native Arc
    net = gross - gas
    with _LOCK:
        st = _pnl_load()
        st["realized_gross_usdc"] = float(st.get("realized_gross_usdc") or 0) + gross
        st["gas_paid_usdc"] = float(st.get("gas_paid_usdc") or 0) + gas
        st["realized_net_usdc"] = float(st.get("realized_net_usdc") or 0) + net
        st["closes"] = int(st.get("closes") or 0) + 1
        st["ts"] = time.time()
        if equity_mark is not None:
            peak = float(st.get("peak_equity_usdc") or equity_mark)
            peak = max(peak, float(equity_mark))
            st["peak_equity_usdc"] = peak
            dd = (peak - float(equity_mark)) / peak if peak > 0 else 0.0
            st["drawdown_pct"] = round(dd, 4)
            if dd >= DRAWDOWN_VALVE_PCT:
                st["valve_tight"] = True
                st["valve_reason"] = f"drawdown {dd:.1%} ≥ {DRAWDOWN_VALVE_PCT:.0%}"
        _pnl_save(st)
        return dict(st)


def realized_snapshot() -> dict[str, Any]:
    with _LOCK:
        return _pnl_load()


def should_tighten_valve(unreal_usdc: float, equity_usdc: float) -> tuple[bool, str]:
    """If floating book drawdown vs peak is violent → close entry valve."""
    with _LOCK:
        st = _pnl_load()
        peak = float(st.get("peak_equity_usdc") or equity_usdc or 0)
        if equity_usdc > peak:
            st["peak_equity_usdc"] = equity_usdc
            st["valve_tight"] = False
            _pnl_save(st)
            return False, ""
        peak = max(peak, equity_usdc)
        if peak <= 0:
            return False, ""
        dd = (peak - equity_usdc) / peak
        # Also react to large negative unreal relative to stake slice
        if unreal_usdc < -abs(peak) * DRAWDOWN_VALVE_PCT:
            st["valve_tight"] = True
            st["valve_reason"] = f"unreal shock {unreal_usdc:.2f}"
            _pnl_save(st)
            return True, st["valve_reason"]
        if dd >= DRAWDOWN_VALVE_PCT:
            st["valve_tight"] = True
            st["valve_reason"] = f"drawdown {dd:.1%}"
            _pnl_save(st)
            return True, st["valve_reason"]
        return bool(st.get("valve_tight")), str(st.get("valve_reason") or "")


def clear_valve_tight() -> None:
    with _LOCK:
        st = _pnl_load()
        st["valve_tight"] = False
        st.pop("valve_reason", None)
        _pnl_save(st)
