"""Grok Bot — trading main loop (isolated asyncio process) ↔ frontend WS.

Architecture
------------
* This module MUST run as its own process (`python -m desk_realtime.trading_loop`).
* Streamlit UI never imports the event loop — it only:
    - drains DeskBus / desk.jsonl
    - writes desk.halt / desk.panic IPC files
* All RPC / LLM I/O is async + hard-timeout'd (httpx). Secrets never hit Desk Feed.

Pipeline (hard stop on any veto):
  runScanner → GoPlus (honeypot only) → runNarrative → runConsensus → runAudit → runRisk → runTiming → executeBuyOrder → runExit

Panic path (UI PANIC SELL / EXIT):
  desk.panic IPC → executeSellOrder(panic=True, slip 15–20%) → Valve Gate CLOSED → halt scans

Run (with hub up):
  PYTHONPATH=grok-trading-desk/.vendor:. \\
    python -m desk_realtime.trading_loop --ws ws://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_VENDOR = _ROOT / "grok-trading-desk" / ".vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from desk_realtime.agent_scoring import score_narrative
from desk_realtime.arc_hunt import capped_entry_usdc, hunt_pool, size_from_wallet
from desk_realtime.crypto_executor import get_executor
from desk_realtime.desk_units import ENTRY, QUOTE
from desk_realtime.engine_state import (
    HALT_PATH,
    consume_panic,
    set_valve,
    valve_closed,
    write_engine_state,
)
from desk_realtime.fastforward import async_pause
from desk_realtime.secrets import redact_text, sanitize_exc

LOG_PATH = _ROOT / "grok-trading-desk" / "logs" / "desk.jsonl"
DEPLOY_PATH = _ROOT / "grok-trading-desk" / "logs" / "desk.deploy"

log = logging.getLogger("trading_loop")

# ---------------------------------------------------------------------------
# Single-position risk (author rule: one position at a time)
# ---------------------------------------------------------------------------

isPositionOpen: bool = False
net_out: int = 0
scouted_count: int = 0
_open: dict[str, Any] | None = None
_wins = 0
_losses = 0


@dataclass
class AgentResult:
    ok: bool
    agent_type: str
    log_text: str
    risk_veto: bool = False
    off_narrative: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def vetoed(self) -> bool:
        return (not self.ok) or self.risk_veto or self.off_narrative


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(row: dict[str, Any]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Belt-and-suspenders: redact any string fields before disk
    safe = {}
    for k, v in row.items():
        safe[k] = redact_text(v) if isinstance(v, str) else v
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(safe, ensure_ascii=False) + "\n")


class DeskPublisher:
    """Publish live packets to the desk WS hub with reconnect + exp backoff."""

    def __init__(self, url: str):
        self.url = url
        self._ws: Any = None
        self._lock = asyncio.Lock()
        self._backoff = 1.0

    async def connect(self) -> None:
        import websockets

        self._ws = await websockets.connect(
            self.url,
            ping_interval=8,
            ping_timeout=16,
            max_size=2_000_000,
            open_timeout=10,
        )
        self._backoff = 1.0
        log.info("publisher connected %s", self.url)

    async def ensure_connected(self) -> None:
        if self._ws is not None:
            return
        while True:
            try:
                await self.connect()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "publisher reconnect in %.1fs: %s",
                    self._backoff,
                    sanitize_exc(exc),
                )
                await asyncio.sleep(self._backoff)
                # Exponential backoff + jitter — avoid RPC/WS ban storms
                jitter = random.uniform(0, self._backoff * 0.25)
                self._backoff = min(self._backoff * 2.0 + jitter, 60.0)

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def emit(self, **packet: Any) -> None:
        """Zero-buffer push: every status change hits the hub immediately."""
        global net_out
        packet.setdefault("timestamp", _now())
        packet.setdefault("net_out", net_out)
        # Never allow secret-bearing fields into the wire
        for bad in ("wallet_key", "private_key", "api_key", "secret", "signer"):
            packet.pop(bad, None)
        if "log_text" in packet:
            packet["log_text"] = redact_text(packet["log_text"])
        raw = json.dumps(packet, ensure_ascii=False)

        await self.ensure_connected()
        try:
            assert self._ws is not None
            await self._ws.send(raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("emit send failed: %s", sanitize_exc(exc))
            await self.close()
            await self.ensure_connected()
            try:
                assert self._ws is not None
                await self._ws.send(raw)
            except Exception as exc2:  # noqa: BLE001
                log.error("emit dropped: %s", sanitize_exc(exc2))

        from desk_realtime.schema import normalize_ws_event

        row = normalize_ws_event(packet)
        if row:
            _append_jsonl(row)
        log.info(
            "emit %s %s | %s",
            packet.get("status"),
            packet.get("token_name"),
            packet.get("log_text"),
        )


CRYPTO_POOL = hunt_pool()


def _live_max_hold() -> float:
    """Paper tape used 8–10s. A live Warp fill holds until the mark rule, not a coin flip."""
    raw = (os.environ.get("ARC_MAX_HOLD_SEC") or "").strip()
    if raw:
        try:
            return max(30.0, float(raw))
        except ValueError:
            pass
    if os.environ.get("ARC_NETWORK", "").strip().lower() == "mainnet":
        return 900.0
    return 10.0



async def runScanner(bus: DeskPublisher, rng: random.Random) -> AgentResult:
    global isPositionOpen, scouted_count
    if isPositionOpen:
        return AgentResult(
            ok=False,
            agent_type="SCANNER",
            log_text="scan skipped · one position at a time",
            data={"skipped": True},
        )

    from desk_realtime.arc_launchpads import pick_scan_target

    target = pick_scan_target(rng)
    name, address = target.symbol, target.address
    try:
        from desk_realtime.arc_dex import market_identity

        ident = await asyncio.to_thread(market_identity, address)
        if ident.get("symbol"):
            name = ident["symbol"]
    except Exception:
        ident = {}
    scouted_count += 1
    fee_note = f" · fee {target.fee_bps}bps" if target.fee_bps else ""
    await bus.emit(
        status="SCAN",
        agent_type="SCANNER",
        token_name=f"${name}",
        entry_size=0,
        log_text=f"scan {target.launchpad} launch ${name}{fee_note}",
        token_address=address,
        scouted_count=scouted_count,
        launchpad=target.launchpad,
        platform_fee_bps=target.fee_bps,
    )
    await async_pause(0.15)
    return AgentResult(
        ok=True,
        agent_type="SCANNER",
        log_text=f"scan {target.launchpad} ${name}",
        data={
            "token": name,
            "token_address": address,
            "launchpad": target.launchpad,
            "platform_fee_bps": target.fee_bps,
        },
    )


async def runNarrative(
    bus: DeskPublisher,
    token: str,
    address: str,
    rng: random.Random,
) -> AgentResult:
    scored = await score_narrative(token, address, rng=rng)
    try:
        from desk_realtime.cluster_book import publish_narrative

        publish_narrative(
            token,
            address,
            {
                "mention_count": scored.get("x_mentions"),
                "engagement": scored.get("x_engagement"),
            },
        )
    except Exception:
        pass
    score = float(scored.get("score") or 0.0)
    off = bool(scored.get("off_narrative") or not scored.get("ok"))
    x_m = scored.get("x_mentions")
    x_note = f" · X {x_m}" if x_m is not None else ""
    await bus.emit(
        status="VOTING",
        agent_type="NARRATIVE",
        token_name=f"${token}",
        entry_size=0.5,
        log_text=(
            "NOT BUY · off-narrative"
            if off
            else f"narrative {score:.2f} theme match ${token}{x_note}"
        ),
        score=score,
        token_address=address,
        x_mentions=x_m,
        source=scored.get("source"),
    )
    await async_pause(0.05)
    return AgentResult(
        ok=not off,
        agent_type="NARRATIVE",
        log_text=f"narrative {score:.2f}",
        off_narrative=off,
        data={"score": score, "token": token, "token_address": address},
    )


async def runConsensus(
    bus: DeskPublisher,
    token: str,
    address: str,
    *,
    llm_score: float | None = None,
) -> AgentResult:
    """Liquidity + cluster + narrative heat — hard veto before TIMING."""
    from desk_realtime.arc_strategy import LIQ_MIN_USDC, run_consensus_filter

    c = run_consensus_filter(token, address, llm_score=llm_score)
    await async_pause(0.05)

    if c.feed_alert:
        await bus.emit(
            status="VETO",
            agent_type="RISK",
            token_name=f"${token}",
            entry_size=0,
            log_text=c.feed_alert,
            token_address=address,
            cluster_score=c.cluster_score,
            liquidity_usdc=c.liquidity_usdc,
        )

    if c.veto:
        reason = "; ".join(c.warnings) or "consensus veto"
        # Surface RISK VETO 100% on liq fail for dock rings / feed
        if c.liq_veto:
            await bus.emit(
                status="VETO",
                agent_type="RISK",
                token_name=f"${token}",
                entry_size=0,
                log_text=(
                    f"RISK VETO 100% · pool {c.liquidity_usdc:.0f} USDC "
                    f"< {LIQ_MIN_USDC:.0f} · BUY CANCELLED"
                ),
                token_address=address,
                risk_veto=True,
                liquidity_usdc=c.liquidity_usdc,
            )
        return AgentResult(
            ok=False,
            agent_type="RISK",
            log_text=reason,
            risk_veto=True,
            data=c.to_dict(),
        )

    await bus.emit(
        status="VOTING",
        agent_type="RISK",
        token_name=f"${token}",
        entry_size=0,
        log_text=(
            f"CONSENSUS clear · liq {c.liquidity_usdc:.0f} USDC · "
            f"cluster {c.cluster_score:.2f} · narr {c.narrative_index:.0f}%"
        ),
        token_address=address,
        narrative_index=c.narrative_index,
        liquidity_usdc=c.liquidity_usdc,
    )
    return AgentResult(
        ok=True,
        agent_type="RISK",
        log_text=f"consensus ok · narr {c.narrative_index:.0f}%",
        data=c.to_dict(),
    )


async def runAudit(
    bus: DeskPublisher,
    token: str,
    address: str,
) -> AgentResult:
    """Arc three-tier gate: REJECT / REVIEW / WATCH. Only WATCH may buy."""
    from desk_realtime.arc_audit import AuditTier, audit_token

    result = await audit_token(token, address, chain="arc")
    await async_pause(0.08)

    if result.tier == AuditTier.WATCH:
        # Surface 可看 on feed without opening a position
        await bus.emit(
            status="VOTING",
            agent_type="RISK",
            token_name=f"${token}",
            entry_size=0,
            log_text=result.log_text,
            token_address=address,
            audit_tier=result.tier.value,
            audit_reason=result.reason,
            audit_source=result.source,
        )
        return AgentResult(
            ok=True,
            agent_type="RISK",
            log_text=result.log_text,
            data=result.to_dict(),
        )

    return AgentResult(
        ok=False,
        agent_type="RISK",
        log_text=result.log_text,
        risk_veto=True,
        data=result.to_dict(),
    )


async def runRisk(
    bus: DeskPublisher,
    token: str,
    address: str,
    narr_score: float,
    rng: random.Random,
) -> AgentResult:
    """Secondary risk pass after consensus + audit (thin-book residual)."""
    from desk_realtime.arc_strategy import LIQ_MIN_USDC, probe_liquidity_usdc

    liq = probe_liquidity_usdc(address)
    depth = min(1.0, float(liq["liquidity_usdc"]) / max(LIQ_MIN_USDC, 1.0))
    # Live book has no dice roll. Narrative below 0.62 already vetoed upstream.
    veto = narr_score < 0.62 or bool(liq.get("veto"))
    del rng
    await async_pause(0.08)
    if veto:
        await bus.emit(
            status="VETO",
            agent_type="RISK",
            token_name=f"${token}",
            entry_size=0,
            log_text="RISK VETO 100% · residual thin-book / narr",
            risk_veto=True,
            token_address=address,
        )
        return AgentResult(
            ok=False,
            agent_type="RISK",
            log_text="book too thin · risk veto",
            risk_veto=True,
            data={"depth": depth, "token": token, "token_address": address, "risk_pct": 100},
        )
    return AgentResult(
        ok=True,
        agent_type="RISK",
        log_text=f"risk clear · depth {depth:.2f} · liq {liq['liquidity_usdc']:.0f} USDC",
        data={"depth": depth, "token": token, "token_address": address, "risk_pct": 0},
    )


async def runTiming(
    bus: DeskPublisher,
    token: str,
    address: str,
    stake: float,
    rng: random.Random,
) -> AgentResult:
    del rng
    from desk_realtime.arc_flow import live_buy_allowed

    ex = _active_executor()
    live = bool(getattr(ex, "live", False))
    gate = live_buy_allowed(address, live=live)
    if live and not gate.get("ok"):
        return AgentResult(
            ok=False,
            agent_type="TIMING",
            log_text=f"timing hold · {gate.get('reason') or 'flow cold'}",
            data={"token": token, "token_address": address, "buyers": gate.get("buyers")},
        )
    if live and hasattr(ex, "roundtrip_ok"):
        try:
            rt = await asyncio.to_thread(ex.roundtrip_ok, address, 0.1)
        except Exception as exc:  # noqa: BLE001
            return AgentResult(
                ok=False,
                agent_type="TIMING",
                log_text=f"timing hold · quote failed · {sanitize_exc(exc)[:80]}",
                data={"token": token, "token_address": address},
            )
        if not rt.get("ok"):
            return AgentResult(
                ok=False,
                agent_type="TIMING",
                log_text=f"timing hold · {rt.get('error') or 'quote failed'}",
                data={"token": token, "token_address": address},
            )
    buyers = gate.get("buyers")
    note = f" · buyers {buyers}" if buyers is not None else ""
    return AgentResult(
        ok=True,
        agent_type="TIMING",
        log_text=f"entry ${token} armed {stake:.2f} {QUOTE}{note}",
        data={
            "token": token,
            "token_address": address,
            "stake": stake,
            "buyers": buyers,
        },
    )


def _active_executor():
    """Route fills to Arc testnet when DESK_CHAIN=arc (default for Arc desk)."""
    chain = os.environ.get("DESK_CHAIN", "arc").strip().lower()
    if chain == "arc":
        from desk_realtime.arc_executor import get_arc_executor

        return get_arc_executor()
    return get_executor()


async def executeBuyOrder(
    token_address: str,
    stake_size: float,
    *,
    symbol: str = "",
    launchpad: str = "",
) -> dict[str, Any]:
    """Buy via Arc. Live fills are 0.5–1 USDC and only after the flow gate."""
    # Cap single bet: min(10% wallet USDC, 50 USDC)
    try:
        sized = size_from_wallet(requested=float(stake_size))
        stake_size = min(float(sized["entry_usdc"]), 1.0)
        if stake_size <= 0:
            return {
                "ok": False,
                "error": "stake capped to 0 · empty wallet or bet limit",
                "dry_run": True,
                "balance_usdc": sized.get("balance_usdc"),
            }
    except Exception as exc:  # noqa: BLE001
        stake_size = capped_entry_usdc(0.0, float(stake_size))
        if stake_size <= 0:
            return {"ok": False, "error": sanitize_exc(exc), "dry_run": True}
        sized = {}

    ex = _active_executor()
    pad = (launchpad or "").strip().lower()
    if pad in ("tolly", "dyor"):
        return {
            "ok": False,
            "error": (
                f"{pad} route blocked · no verified mainnet swap ABI on this desk · "
                "refusing cast send"
            ),
            "dry_run": True,
        }
    if getattr(ex, "live", False):
        from desk_realtime.arc_flow import live_buy_allowed

        gate = live_buy_allowed(token_address, live=True)
        if not gate.get("ok"):
            return {
                "ok": False,
                "error": f"flow gate · {gate.get('reason')}",
                "dry_run": True,
                "balance_usdc": sized.get("balance_usdc"),
            }
    try:
        return await asyncio.wait_for(
            ex.buy(
                token_address,
                stake_size,
                symbol=symbol,
                launchpad=launchpad,
            ),
            timeout=20.0,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": "buy timeout", "dry_run": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": sanitize_exc(exc), "dry_run": True}


async def executeSellOrder(
    token_address: str,
    *,
    symbol: str = "",
    fraction: float = 1.0,
    panic: bool = False,
    trailing: bool = False,
    launchpad: str = "",
    token_amount: int = 0,
    min_usdc_out: int = 0,
) -> dict[str, Any]:
    """Market flatten — normal / trailing / PANIC (Arc USDC slip 15–20% on urgency)."""
    ex = _active_executor()
    try:
        return await asyncio.wait_for(
            ex.sell(
                token_address,
                token_amount=int(token_amount or 0),
                symbol=symbol,
                fraction=fraction,
                panic=panic,
                trailing=trailing,
                launchpad=launchpad,
                min_usdc_out=int(min_usdc_out or 0),
            ),
            timeout=12.0 if panic else 45.0,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": "sell timeout", "panic": panic, "trailing": trailing}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": sanitize_exc(exc), "panic": panic, "trailing": trailing}


async def runExit(
    bus: DeskPublisher,
    position: dict[str, Any],
    rng: random.Random,
) -> AgentResult:
    """Monitor open book; fire exit when multiple / trailing / time rule hits."""
    global isPositionOpen, _open, _wins, _losses

    held = time.time() - float(position.get("opened_at") or time.time())
    entry = float(position.get("entry_size") or position.get("cost_usdc") or 0.0)
    cost = float(position.get("cost_usdc") or entry)
    curve = str(position.get("token_address") or "")
    token_amt = int(position.get("token_amount") or 0)

    # On-chain mark via quoteSell; fall back to last base_mult only if quote fails
    mark_src = "sim"
    live_mult = 0.0
    mark_usdc = 0.0
    try:
        ex = _active_executor()
        if hasattr(ex, "mark_position") and curve.startswith("0x"):
            mark = await asyncio.to_thread(
                ex.mark_position,
                curve,
                token_amount=token_amt,
                cost_usdc=cost,
            )
            if float(mark.get("live_mult") or 0) > 0:
                live_mult = float(mark["live_mult"])
                mark_usdc = float(mark.get("mark_usdc") or 0)
                mark_src = str(mark.get("source") or "quoteSell")
                if int(mark.get("token_amount") or 0) > 0:
                    position["token_amount"] = int(mark["token_amount"])
                    if _open is not None:
                        _open["token_amount"] = int(mark["token_amount"])
    except Exception as exc:  # noqa: BLE001
        log.info("mark_position failed: %s", sanitize_exc(exc))

    if live_mult <= 0:
        # Last-resort sim (should be rare) — do not invent runaway upside
        base = float(position.get("base_mult") or 1.0)
        live_mult = round(max(0.5, min(base, 1.05)), 4)
        mark_src = "sim_fallback"

    live_mult = round(float(live_mult), 4)
    peak = float(position.get("peak_mult") or live_mult)
    peak = max(peak, live_mult)
    position["peak_mult"] = peak
    if _open is not None:
        _open["peak_mult"] = peak
        _open["live_mult"] = live_mult
        _open["mark_usdc"] = mark_usdc

    # Trailing stop: give back ≥12% from peak mark → widen slip & smash out
    trail_dd = (peak - live_mult) / peak if peak > 0 else 0.0
    trailing_hit = peak >= 1.35 and trail_dd >= 0.12

    await bus.emit(
        status="BUY",
        agent_type="TIMING",
        token_name=position["token_name"],
        entry_size=position["entry_size"],
        mult=live_mult,
        log_text=(
            f"HOLD {position['token_name']} · {live_mult:.2f}x mark ({mark_src})"
            + (f" · ${mark_usdc:.2f}" if mark_usdc > 0 else "")
            + (f" · trail −{trail_dd:.0%} from {peak:.2f}x" if trail_dd >= 0.05 else "")
        ),
        token_address=position.get("token_address"),
        score=position.get("score", 0.88),
    )

    # Unrealized shock → tighten Valve Gate (block new buys)
    try:
        from desk_realtime.arc_strategy import should_tighten_valve

        unreal = (mark_usdc - cost) if mark_usdc > 0 else entry * (live_mult - 1.0)
        equity = (mark_usdc if mark_usdc > 0 else entry * live_mult)
        tight, why = should_tighten_valve(unreal, equity)
        if tight:
            set_valve(True)
            await bus.emit(
                status="HOLD_OFF",
                agent_type="RISK",
                token_name=position["token_name"],
                entry_size=entry,
                log_text=f"VALVE TIGHT · {why or 'drawdown'} · auto-buy paused",
            )
    except Exception:
        pass

    # Also exit if mark < 0.85x (hard stop) or graduated bag dump via time
    hard_stop = live_mult > 0 and live_mult <= 0.85 and mark_src != "sim_fallback"
    should_exit = (
        held >= float(position.get("max_hold_sec") or _live_max_hold())
        or live_mult >= 4.0
        or trailing_hit
        or hard_stop
    )
    if not should_exit:
        return AgentResult(
            ok=True,
            agent_type="EXIT",
            log_text=f"watching · {live_mult:.2f}x open ({mark_src})",
            data={"mult": live_mult, "held": held, "peak": peak, "mark_src": mark_src},
        )

    fill = await executeSellOrder(
        curve,
        symbol=str(position.get("token_name") or "").lstrip("$"),
        fraction=1.0,
        panic=hard_stop,
        trailing=trailing_hit,
        launchpad=str(position.get("launchpad") or ""),
        token_amount=int(position.get("token_amount") or 0),
    )
    if not fill.get("ok"):
        await bus.emit(
            status="EXIT",
            agent_type="EXIT",
            token_name=position["token_name"],
            entry_size=position["entry_size"],
            mult=live_mult,
            log_text=f"ERROR · exit rpc failed · {fill.get('error') or 'unknown'}",
        )
        return AgentResult(
            ok=False,
            agent_type="EXIT",
            log_text="exit failed",
            data={"error": fill.get("error")},
        )

    if live_mult >= 1.0:
        _wins += 1
    else:
        _losses += 1

    # Realized from mark when available
    pnl_net = (mark_usdc - cost) if mark_usdc > 0 else entry * (float(live_mult) - 1.0)
    try:
        from desk_realtime.arc_strategy import GAS_ESTIMATE_USDC, record_close_pnl
        from desk_realtime.llm_budget import credit_from_pnl

        gas = GAS_ESTIMATE_USDC * 2.0  # buy+sell round-trip on Arc USDC
        ledger = record_close_pnl(
            entry_usdc=cost or entry,
            exit_mult=float(live_mult),
            gas_usdc=gas,
            equity_mark=mark_usdc if mark_usdc > 0 else entry * float(live_mult),
        )
        pnl_net = pnl_net - gas
        credit_from_pnl(pnl_net)
        if ledger.get("valve_tight"):
            set_valve(True)
    except Exception:
        pass

    slip_note = ""
    if trailing_hit or fill.get("slippage_bps"):
        slip_note = f" · slip {int(fill.get('slippage_bps') or 0)}bps"
    if hard_stop:
        mode = "HARD STOP"
    elif trailing_hit:
        mode = "TRAIL EXIT"
    else:
        mode = "exit fired"
    await bus.emit(
        status="EXIT",
        agent_type="EXIT",
        token_name=position["token_name"],
        entry_size=position["entry_size"],
        mult=live_mult,
        pnl=pnl_net,
        log_text=f"{mode} · {live_mult:.2f}x ({mark_src}) · net {pnl_net:+.3f} USDC{slip_note}",
        token_address=position.get("token_address"),
        tx_id=fill.get("tx_id"),
        trailing=trailing_hit,
        slippage_bps=fill.get("slippage_bps"),
    )
    isPositionOpen = False
    _open = None
    return AgentResult(
        ok=True,
        agent_type="EXIT",
        log_text=f"{mode} · {live_mult:.2f}x locked",
        data={
            "mult": live_mult,
            "closed": True,
            "tx_id": fill.get("tx_id"),
            "pnl_net": pnl_net,
            "trailing": trailing_hit,
            "mark_src": mark_src,
            "hard_stop": hard_stop,
        },
    )


async def execute_panic_flatten(bus: DeskPublisher, req: dict[str, Any] | None = None) -> None:
    """
    PANIC SELL / EXIT — bypass ALL agent scoring.
    Market-flatten open book + close Valve Gate (halt auto-buy).
    """
    global isPositionOpen, _open, _wins, _losses

    set_valve(True)  # Valve Gate CLOSED + desk.halt
    req = req or {}
    pos = _open
    sym = str(
        (pos or {}).get("token_name")
        or req.get("symbol")
        or "UNK"
    ).lstrip("$")
    addr = str(
        (pos or {}).get("token_address")
        or req.get("token_address")
        or ""
    )

    await bus.emit(
        status="HOLD_OFF",
        agent_type="EXIT",
        token_name=f"${sym}",
        entry_size=float((pos or {}).get("entry_size") or 0.5),
        log_text=f"PANIC · valve CLOSED · flattening ${sym}",
        op="halt",
    )

    if not pos and not addr:
        await bus.emit(
            status="HOLD_OFF",
            agent_type="EXIT",
            token_name="$DESK",
            entry_size=0.5,
            log_text="PANIC · no open book · scanning halted",
        )
        return

    fill = await executeSellOrder(
        addr,
        symbol=sym,
        fraction=1.0,
        panic=True,
        launchpad=str((_open or {}).get("launchpad") or ""),
        token_amount=int((_open or {}).get("token_amount") or 0),
    )
    mult = float((pos or {}).get("base_mult") or 1.0)
    entry = float((pos or {}).get("entry_size") or 0.5)
    pnl_net = entry * (mult - 1.0)
    if fill.get("ok"):
        if mult >= 1.0:
            _wins += 1
        else:
            _losses += 1
        try:
            from desk_realtime.arc_strategy import GAS_ESTIMATE_USDC, record_close_pnl
            from desk_realtime.llm_budget import credit_from_pnl

            gas = GAS_ESTIMATE_USDC * 2.0
            record_close_pnl(
                entry_usdc=entry,
                exit_mult=mult,
                gas_usdc=gas,
                equity_mark=entry * mult,
            )
            pnl_net = entry * (mult - 1.0) - gas
            credit_from_pnl(pnl_net)
        except Exception:
            pass
        slip = int(fill.get("slippage_bps") or 0)
        await bus.emit(
            status="EXIT",
            agent_type="EXIT",
            token_name=f"${sym}",
            entry_size=entry,
            mult=mult,
            pnl=pnl_net,
            log_text=(
                f"PANIC SELL ${sym} · market flatten · "
                f"net {pnl_net:+.3f} USDC · slip {slip}bps · auto-buy paused"
            ),
            token_address=addr,
            tx_id=fill.get("tx_id"),
            panic=True,
            slippage_bps=slip,
        )
    else:
        await bus.emit(
            status="EXIT",
            agent_type="EXIT",
            token_name=f"${sym}",
            entry_size=float((pos or {}).get("entry_size") or 0.5),
            mult=mult,
            log_text=f"ERROR · PANIC sell failed · {fill.get('error') or 'rpc'}",
            panic=True,
        )

    isPositionOpen = False
    _open = None


async def _reject(
    bus: DeskPublisher,
    token: str,
    agent: str,
    log_text: str,
    *,
    risk_veto: bool = False,
    off_narrative: bool = False,
) -> None:
    global net_out
    net_out += 1
    await bus.emit(
        status="VETO",
        agent_type=agent,
        token_name=f"${token}" if not str(token).startswith("$") else token,
        entry_size=0.5,
        log_text=redact_text(log_text),
        net_out=net_out,
        net_out_delta=1,
        risk_veto=risk_veto,
        off_narrative=off_narrative,
    )


async def run_agent_pipeline(
    bus: DeskPublisher,
    stake: float,
    rng: random.Random,
) -> None:
    global isPositionOpen, _open

    if isPositionOpen or valve_closed():
        return

    scan = await runScanner(bus, rng)
    if scan.data.get("skipped") or not scan.ok:
        return

    token = str(scan.data["token"])
    address = str(scan.data["token_address"])
    launchpad = str(scan.data.get("launchpad") or "")

    try:
        from desk_realtime.arc_goplus import scan_token

        gp = await scan_token(address, symbol=token)
        if gp.get("honeypot"):
            await bus.emit(
                status="VETO",
                agent_type="RISK",
                token_name=f"${token}",
                entry_size=0,
                log_text=str(gp.get("line") or "GOPLUS · buy blocked"),
                token_address=address,
                risk_veto=True,
                goplus=True,
            )
            await _reject(
                bus,
                token,
                "RISK",
                str(gp.get("line") or "GOPLUS · buy blocked"),
                risk_veto=True,
            )
            return
        if gp.get("provider_na"):
            await bus.emit(
                status="VOTING",
                agent_type="RISK",
                token_name=f"${token}",
                entry_size=0,
                log_text="GOPLUS n/a · chain 5042 · sell check is on-chain quote",
                token_address=address,
            )
        elif not gp.get("buy"):
            await _reject(
                bus,
                token,
                "RISK",
                str(gp.get("line") or "GOPLUS · buy blocked"),
                risk_veto=True,
            )
            return
    except Exception as exc:  # noqa: BLE001
        await _reject(bus, token, "RISK", f"GOPLUS scan failed · buy blocked · {exc}", risk_veto=True)
        return

    narr = await runNarrative(bus, token, address, rng)
    if narr.vetoed:
        await _reject(
            bus, token, "NARRATIVE",
            narr.log_text if narr.off_narrative else "NOT BUY · off-narrative",
            off_narrative=True,
        )
        return

    # Agent consensus: liq depth · wallet cluster · narrative heat (hard veto)
    consensus = await runConsensus(
        bus,
        token,
        address,
        llm_score=float(narr.data.get("score") or 0),
    )
    if consensus.vetoed:
        await _reject(bus, token, "RISK", consensus.log_text, risk_veto=True)
        return

    # Arc three-tier audit (拒绝 / 待复核 / 可看) — unknown = REVIEW, never auto-buy
    audit = await runAudit(bus, token, address)
    if audit.vetoed:
        await _reject(bus, token, "RISK", audit.log_text, risk_veto=True)
        return

    risk = await runRisk(bus, token, address, float(narr.data["score"]), rng)
    if risk.vetoed:
        await _reject(bus, token, "RISK", "book too thin · risk veto", risk_veto=True)
        return

    # EDGE MODEL — Tolly 1% lock subtracted before arming
    try:
        from desk_realtime.arc_strategy import edge_model_r

        edge = edge_model_r(
            float(narr.data.get("score") or 0.55),
            1.8,
            1.0,
            launchpad=launchpad,
            address=address,
            symbol=token,
        )
        await bus.emit(
            status="VOTING",
            agent_type="TIMING",
            token_name=f"${token}",
            entry_size=stake,
            log_text=(
                f"EDGE {edge['expectancy_r']:+.3f}R · {launchpad or 'pad'} "
                f"fee {edge['fee_bps']}bps"
                + ("" if edge.get("accepted") else " · rejected")
            ),
            expectancy=edge.get("expectancy_r"),
            launchpad=launchpad,
        )
        if not edge.get("accepted"):
            await _reject(
                bus, token, "TIMING",
                f"EDGE rejected · {edge['expectancy_r']:+.3f}R after pad fee",
            )
            return
    except Exception:
        pass

    timing = await runTiming(bus, token, address, stake, rng)
    if timing.vetoed:
        await _reject(bus, token, "TIMING", timing.log_text, risk_veto=False)
        return

    fill = await executeBuyOrder(address, stake, symbol=token, launchpad=launchpad)
    if not fill.get("ok"):
        await _reject(
            bus, token, "TIMING",
            f"buy failed · {fill.get('error') or 'rpc/signer'}",
            risk_veto=True,
        )
        return

    isPositionOpen = True
    _open = {
        "token_name": f"${token}",
        "token_address": address,
        "entry_size": stake,
        "cost_usdc": float(fill.get("cost_usdc") or stake),
        "token_amount": int(fill.get("token_amount") or 0),
        "meme_token": str(fill.get("meme_token") or ""),
        "opened_at": time.time(),
        "base_mult": 1.0,
        "score": float(narr.data["score"]),
        "tx_id": fill.get("tx_id"),
        "max_hold_sec": _live_max_hold(),
        "dry_run": bool(fill.get("dry_run", True)),
        "launchpad": launchpad,
        "slippage_bps": fill.get("slippage_bps"),
    }
    await bus.emit(
        status="BUY",
        agent_type="TIMING",
        token_name=f"${token}",
        entry_size=stake,
        log_text=(
            f"BUY ${token} via {launchpad or 'pad'} — "
            f"slip {int(fill.get('slippage_bps') or 0)}bps"
        ),
        token_address=address,
        score=float(narr.data["score"]),
        tx_id=fill.get("tx_id"),
        mult=_open["base_mult"],
        launchpad=launchpad,
    )


async def emit_metrics(bus: DeskPublisher, boot: float) -> None:
    total = _wins + _losses
    wr = (_wins / total * 100.0) if total else 0.0
    up = max(0, int(time.time() - boot))
    halted = valve_closed()
    realized_net = 0.0
    try:
        from desk_realtime.arc_strategy import realized_snapshot

        realized_net = float(realized_snapshot().get("realized_net_usdc") or 0.0)
    except Exception:
        pass
    payload = {
        "op": "metrics",
        "status": "METRICS",
        "win_rate": round(wr, 1),
        "scouted_count": scouted_count,
        "net_out": net_out,
        "books": _wins + _losses,
        "trench": scouted_count,
        "not_buy": net_out,
        "agents_live": 0 if halted else 5,
        "uptime_sec": up,
        "day": max(1, 1 + up // 86400),
        "desk_mode": "HALTED" if halted else ("OPEN" if isPositionOpen else "LIVE"),
        "is_position_open": isPositionOpen,
        "multiple": (float(_open["base_mult"]) if _open else 0.0),
        "valve_gate": "CLOSED" if halted else "OPEN",
        "realized_net_usdc": realized_net,
        "pnl": realized_net,
    }
    await bus.emit(**payload)
    # Local IPC cache for UI (no RPC from Streamlit)
    write_engine_state({
        "desk_mode": payload["desk_mode"],
        "valve_gate": payload["valve_gate"],
        "is_position_open": isPositionOpen,
        "open_symbol": (_open or {}).get("token_name"),
        "open_address": (_open or {}).get("token_address"),
        "open_mult": (_open or {}).get("base_mult"),
        "net_out": net_out,
        "scouted_count": scouted_count,
        "win_rate": payload["win_rate"],
        "dry_run": bool((_open or {}).get("dry_run", False)),
        "realized_net_usdc": realized_net,
    })


async def trading_main_loop(
    ws_url: str,
    stake: float = 0.5,
    scan_every: float = 2.5,
    exit_every: float = 1.0,
) -> None:
    global isPositionOpen, _open, net_out

    rng = random.Random()
    bus = DeskPublisher(ws_url)
    await bus.ensure_connected()
    boot = time.time()

    # Probe RPC once (async, timed) — never blocks Streamlit
    health = await get_executor().health_check()
    await bus.emit(
        status="INIT",
        agent_type="EXIT",
        token_name="$DESK",
        entry_size=stake,
        log_text=(
            "trading loop armed · one position at a time"
            + (" · rpc ok" if health.get("ok") else " · rpc degraded")
        ),
        net_out=net_out,
    )
    await emit_metrics(bus, boot)

    factory_every = float(os.environ.get("ARC_FACTORY_POLL_SEC", "8"))
    last_factory = 0.0
    last_dex = 0.0
    pads_announced = False

    try:
        while True:
            now = time.time()
            if now - last_factory >= factory_every:
                last_factory = now
                try:
                    from desk_realtime.arc_factory import poll_loop_once, status as factory_status

                    fresh = poll_loop_once()
                    st = factory_status()
                    if not pads_announced:
                        pads_announced = True
                        pads = st.get("pads") or {}
                        await bus.emit(
                            status="SCAN",
                            agent_type="SCANNER",
                            token_name="$PADS",
                            entry_size=0,
                            log_text=(
                                "listeners · warp "
                                + str(pads.get("warp") or "dark")
                                + " · tolly "
                                + str(pads.get("tolly") or "dark")
                                + " · dyor "
                                + str(pads.get("dyor") or "dark")
                            ),
                        )
                    if fresh:
                        await bus.emit(
                            status="SCAN",
                            agent_type="SCANNER",
                            token_name="$FACTORY",
                            entry_size=0,
                            log_text=f"factory +{len(fresh)} curve(s) · {st.get('factory') or 'n/a'}",
                        )
                except Exception:
                    pass

            # —— Panic IPC (UI PANIC SELL) — highest priority, bypass agents ——
            panic_req = consume_panic()
            if panic_req is not None:
                await execute_panic_flatten(bus, panic_req)
                await emit_metrics(bus, boot)
                await async_pause(0.5)
                continue

            if now - last_dex >= 45:
                last_dex = now
                try:
                    from desk_realtime.arc_dex import dex_enabled, sync_watch

                    if dex_enabled():
                        book = sync_watch()
                        bits = []
                        for row in book.get("rows") or []:
                            sym = row.get("symbol") or "curve"
                            if row.get("indexed"):
                                bits.append(f"{sym} {float(row.get('liquidity_usdc') or 0):.0f}")
                        for row in book.get("tape") or []:
                            sym = row.get("symbol") or ""
                            if sym and sym not in " ".join(bits):
                                bits.append(f"{sym} {float(row.get('liquidity_usdc') or 0):.0f}")
                        if not bits:
                            bits.append("arc tape unread")
                        if bits:
                            await bus.emit(
                                status="SCAN",
                                agent_type="SCANNER",
                                token_name="$DEX",
                                entry_size=0,
                                log_text="dexscreener arc · " + " · ".join(bits[:4]),
                            )
                except Exception:
                    pass

            # Valve Gate / emergency halt — pause scan; still allow exit marks
            if valve_closed() or HALT_PATH.exists():
                if isPositionOpen and _open is not None:
                    # Halt blocks NEW buys but still publish marks (no auto exit on halt
                    # unless panic). Keep book visible.
                    await bus.emit(
                        status="BUY",
                        agent_type="EXIT",
                        token_name=_open["token_name"],
                        entry_size=_open["entry_size"],
                        mult=_open.get("base_mult"),
                        log_text=f"HALT · holding {_open['token_name']} · valve CLOSED",
                        token_address=_open.get("token_address"),
                    )
                else:
                    await bus.emit(
                        status="HOLD_OFF",
                        agent_type="EXIT",
                        token_name="$DESK",
                        entry_size=stake,
                        log_text="EMERGENCY STOP · scanning paused · valve CLOSED",
                    )
                await emit_metrics(bus, boot)
                await async_pause(1.0)
                continue

            if isPositionOpen and _open is not None:
                await runExit(bus, _open, rng)
                await emit_metrics(bus, boot)
                await async_pause(exit_every)
                continue

            await run_agent_pipeline(bus, stake=stake, rng=rng)
            await emit_metrics(bus, boot)
            await async_pause(scan_every)
    finally:
        await bus.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    p = argparse.ArgumentParser(description="Grok bot trading loop → desk WS")
    p.add_argument("--ws", default="ws://127.0.0.1:8765")
    p.add_argument("--stake", type=float, default=ENTRY, help=f"per-fill size in {QUOTE}")
    p.add_argument("--scan-every", type=float, default=2.5)
    p.add_argument("--exit-every", type=float, default=1.0)
    args = p.parse_args()
    asyncio.run(
        trading_main_loop(
            ws_url=args.ws,
            stake=args.stake,
            scan_every=args.scan_every,
            exit_every=args.exit_every,
        )
    )


if __name__ == "__main__":
    main()
