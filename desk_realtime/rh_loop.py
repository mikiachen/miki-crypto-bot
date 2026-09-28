"""Robinhood Chain short-term trading loop. Arc stays off.

Pipeline: board scan → 5m/15m gates → quote → local-sign buy → hard-stop / max-hold exits.
Broadcast only when RH_UNI_SEND=1. Paper/dry otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_VENDOR = _ROOT / "grok-trading-desk" / ".vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from desk_realtime.engine_state import (
    HALT_PATH,
    consume_panic,
    set_valve,
    valve_closed,
    write_engine_state,
)
from desk_realtime.rh_net import USDG, preflight, wait_receipt, wallet
from desk_realtime.rh_strategy import (
    day_loss_halt,
    ema_stop_pct,
    equity_ta_filter_enabled,
    format_audit,
    hard_stop,
    intraday_enabled,
    liquid_board,
    liquid_universe_label,
    max_book_usdg,
    max_hold_sec,
    max_premium_pct,
    pick_short_target,
    portfolio_block,
    quiet_reason,
    realized_snapshot,
    record_close_pnl,
    rsi_exit_level,
    session_label,
    session_swing_enabled,
    should_exit,
    take_profit,
    trade_block,
    underlying_daily_ta,
    universe_mode,
)
from desk_realtime.rh_ledger import note_buy, note_gas, note_sell, usdg_flow
from desk_realtime.rh_uniswap import (
    erc20_balance_read,
    execute_buy,
    execute_gas_refuel,
    execute_sell,
    gas_min_eth,
    gas_refuel_enabled,
    gas_refuel_usdg,
    hard_cap,
    quote_held_mark,
    quote_roundtrip,
    send_armed,
)
from desk_realtime.secrets import redact_text, sanitize_exc

LOG_PATH = _ROOT / "grok-trading-desk" / "logs" / "desk.jsonl"
LOOP_LOG = _ROOT / "grok-trading-desk" / "logs" / "rh_loop.log"
_BOOK_PATH = _ROOT / "grok-trading-desk" / "logs" / "rh_open_book.json"
_COOL_PATH = _ROOT / "grok-trading-desk" / "logs" / "rh_reentry_cool.json"
_ALERT_PATH = _ROOT / "grok-trading-desk" / "logs" / "desk.alert"

log = logging.getLogger("rh_loop")

_book: list[dict[str, Any]] = []
_wins = 0
_losses = 0
_scouted = 0
_skips = 0
net_out = 0
_cool_until: dict[str, float] = {}
_alert_until: dict[str, float] = {}
_last_gas_refuel = 0.0


def alert_cooldown_sec() -> float:
    try:
        return max(60.0, float(os.environ.get("RH_ALERT_COOLDOWN_SEC", "900")))
    except ValueError:
        return 900.0


def sell_fail_cooldown_sec() -> float:
    """Pause between sell broadcasts after a failure (stops 2s gas spam)."""
    try:
        return max(5.0, float(os.environ.get("RH_SELL_FAIL_COOLDOWN_SEC", "45")))
    except ValueError:
        return 45.0


def sell_fail_max_chain() -> int:
    """Max on-chain sell reverts per open bag before a long cool."""
    try:
        return max(1, int(os.environ.get("RH_SELL_FAIL_MAX_CHAIN", "3")))
    except ValueError:
        return 3


def gas_refuel_cooldown_sec() -> float:
    try:
        return max(60.0, float(os.environ.get("RH_GAS_REFUEL_COOLDOWN_SEC", "600")))
    except ValueError:
        return 600.0


async def _maybe_refuel_gas(bus: DeskPublisher, *, force: bool = False) -> None:
    """When native ETH is thin, spend ~1–2 USDG → WETH → unwrap. No spam."""
    global _last_gas_refuel
    if not send_armed() or not gas_refuel_enabled():
        return
    now = time.time()
    if not force and (now - _last_gas_refuel) < gas_refuel_cooldown_sec():
        return
    snap = _refresh_wallet_snap(force=True)
    eth = float(snap.get("eth") or 0.0)
    if eth >= gas_min_eth():
        return
    _last_gas_refuel = now
    size = gas_refuel_usdg()
    await bus.emit(
        status="SCAN",
        agent_type="RISK",
        token_name="$GAS",
        entry_size=size,
        log_text=f"gas thin {eth:.6f} ETH · refuel {size:.2f} USDG",
    )
    result = await asyncio.to_thread(execute_gas_refuel, usdg_in=size)
    if result.get("skipped"):
        await bus.emit(
            status="HOLD",
            agent_type="RISK",
            token_name="$GAS",
            entry_size=size,
            log_text=f"gas refuel skip · {result.get('reason') or result.get('error')}",
        )
        return
    if result.get("ok"):
        _refresh_wallet_snap(force=True)
        await bus.emit(
            status="BUY",
            agent_type="RISK",
            token_name="$GAS",
            entry_size=size,
            log_text=(
                f"gas refuel ok · {size:.2f} USDG → ETH "
                f"{float(result.get('eth_before') or eth):.6f}→{float(result.get('eth_after') or 0):.6f} · "
                f"tx {str(result.get('tx_id') or '')[:12]}"
            ),
        )
        try:
            note_gas(
                symbol="GAS",
                token=USDG,
                reason=f"refuel {size:.2f} USDG → ETH",
                tx_id=str(result.get("tx_id") or result.get("swap_tx") or ""),
                gas_eth=float(result.get("gas_eth") or 0),
                side_hint="refuel",
            )
        except Exception:
            pass
        return
    await bus.emit(
        status="VETO",
        agent_type="RISK",
        token_name="$GAS",
        entry_size=size,
        log_text=f"gas refuel fail · {result.get('error')}",
    )
    if _ops_alert_key(str(result.get("error") or "")):
        await raise_ops_alert(
            bus,
            key="gas_eth",
            title="ETH gas empty",
            detail=str(result.get("error") or "refuel failed"),
            token_name="$GAS",
        )


def _ops_alert_key(err: str) -> str:
    text = (err or "").lower()
    if "insufficient funds" in text or "have " in text and "want " in text:
        return "gas_eth"
    if "nonce too low" in text:
        return "nonce"
    return ""


def _mac_notify(title: str, body: str) -> None:
    """Best-effort macOS banner. Never raises into the trading loop."""
    try:
        safe_title = title.replace('"', "'")[:80]
        safe_body = body.replace('"', "'")[:180]
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{safe_body}" with title "{safe_title}" sound name "Sosumi"',
            ],
            check=False,
            timeout=5,
            capture_output=True,
        )
    except Exception:
        log.debug("mac notify skipped", exc_info=True)


async def raise_ops_alert(
    bus: DeskPublisher,
    *,
    key: str,
    title: str,
    detail: str,
    token_name: str = "$DESK",
) -> bool:
    """Desk feed + sticky file + macOS banner. Rate-limited per key."""
    if not key:
        return False
    now = time.time()
    if now < float(_alert_until.get(key) or 0):
        return False
    _alert_until[key] = now + alert_cooldown_sec()
    row = {
        "ts": now,
        "key": key,
        "title": title,
        "detail": detail[:400],
        "token": token_name,
    }
    try:
        _ALERT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ALERT_PATH.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    except Exception:
        log.exception("desk.alert write failed")
    await bus.emit(
        status="HOLD_OFF",
        agent_type="RISK",
        token_name=token_name,
        entry_size=0,
        log_text=f"ALERT · {title} · {detail}",
    )
    await asyncio.to_thread(_mac_notify, f"miki rh · {title}", detail)
    return True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cooled(addr: str) -> bool:
    return time.time() < float(_cool_until.get(addr.lower()) or 0)


def reentry_sec() -> float:
    """After a full close, do not buy that token again inside one signal window."""
    try:
        return max(0.0, float(os.environ.get("RH_REENTRY_SEC", "900")))
    except ValueError:
        return 900.0


def cooled_addresses() -> set[str]:
    now = time.time()
    return {addr for addr, until in _cool_until.items() if float(until) > now}


def _persist_cool() -> None:
    now = time.time()
    live = {addr: until for addr, until in _cool_until.items() if float(until) > now}
    try:
        _COOL_PATH.parent.mkdir(parents=True, exist_ok=True)
        _COOL_PATH.write_text(json.dumps(live), encoding="utf-8")
    except Exception:
        log.exception("reentry cool persist failed")


def _restore_cool() -> None:
    if not _COOL_PATH.is_file():
        return
    try:
        raw = json.loads(_COOL_PATH.read_text(encoding="utf-8"))
    except Exception:
        log.exception("reentry cool restore failed")
        return
    if not isinstance(raw, dict):
        return
    now = time.time()
    for addr, until in raw.items():
        try:
            left = float(until)
        except (TypeError, ValueError):
            continue
        if left > now and addr:
            _cool_until[str(addr).lower()] = left


def _cool_set(addr: str, seconds: float) -> None:
    if not addr:
        return
    _cool_until[addr.lower()] = time.time() + max(0.0, seconds)
    _persist_cool()


def _append_jsonl(row: dict[str, Any]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    safe = {k: redact_text(v) if isinstance(v, str) else v for k, v in row.items()}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(safe, ensure_ascii=False) + "\n")


class DeskPublisher:
    def __init__(self, url: str):
        self.url = url
        self._ws: Any = None
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
                log.warning("publisher reconnect in %.1fs: %s", self._backoff, sanitize_exc(exc))
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2.0 + random.uniform(0, 0.5), 60.0)

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def emit(self, **packet: Any) -> None:
        global net_out
        packet.setdefault("timestamp", _now())
        packet.setdefault("net_out", net_out)
        for bad in ("wallet_key", "private_key", "api_key", "secret", "signer"):
            packet.pop(bad, None)
        if "log_text" in packet:
            packet["log_text"] = redact_text(str(packet["log_text"]))
        raw = json.dumps(packet, ensure_ascii=False)
        try:
            await self.ensure_connected()
            assert self._ws is not None
            await self._ws.send(raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("emit send failed: %s", sanitize_exc(exc))
            await self.close()
        from desk_realtime.schema import normalize_ws_event

        row = normalize_ws_event(packet)
        if row:
            _append_jsonl(row)
        log.info("emit %s %s | %s", packet.get("status"), packet.get("token_name"), packet.get("log_text"))


def max_slots() -> int:
    try:
        return max(1, min(10, int(os.environ.get("RH_MAX_SLOTS", "1"))))
    except ValueError:
        return 1


def held_addresses() -> set[str]:
    return {str(p.get("token_address") or "").lower() for p in _book if p.get("token_address")}


# UI reads these from engine_state — avoid Streamlit → RPC on every fragment tick.
_wallet_snap: dict[str, Any] = {
    "ts": 0.0,
    "usdg": 0.0,
    "eth": 0.0,
    "block": 0,
}


def _refresh_wallet_snap(*, force: bool = False) -> dict[str, Any]:
    """Throttle USDG/ETH/block reads for the desk UI cache."""
    global _wallet_snap
    try:
        ttl = float(os.environ.get("RH_STATE_WALLET_TTL", "20") or 20)
    except ValueError:
        ttl = 20.0
    now = time.time()
    if not force and _wallet_snap.get("ts") and (now - float(_wallet_snap["ts"])) < ttl:
        return _wallet_snap
    who = wallet()
    usdg = float(_wallet_snap.get("usdg") or 0.0)
    eth = float(_wallet_snap.get("eth") or 0.0)
    block = int(_wallet_snap.get("block") or 0)
    try:
        from desk_realtime.rh_uniswap import erc20_balance_read

        ok, raw = erc20_balance_read(USDG, who, timeout=3.0)
        if ok:
            usdg = float(raw) / 1_000_000.0
    except Exception:
        pass
    try:
        from desk_realtime.rh_net import fetch_eth_balance, rpc_json

        bal = fetch_eth_balance(who, timeout=3.0)
        if bal.get("ok"):
            eth = float(bal.get("eth") or 0.0)
        blk = rpc_json("eth_blockNumber", timeout=3.0, max_endpoints=1)
        if isinstance(blk, str) and blk.startswith("0x"):
            block = int(blk, 16)
    except Exception:
        pass
    _wallet_snap = {
        "ts": now,
        "usdg": round(usdg, 6),
        "eth": eth,
        "block": block,
    }
    return _wallet_snap


def _write_state(*, boot: float) -> None:
    snap = realized_snapshot()
    pump_bits: dict[str, Any] = {}
    try:
        from desk_realtime.pump_intel import read_intel

        intel = read_intel()
        pump_bits = {
            "pump_themes": [t.get("theme") for t in (intel.get("themes") or [])[:5]],
            "pump_hot": [
                str(h.get("symbol") or "")
                for h in (intel.get("hot") or [])[:3]
            ],
            "pump_updated_at": intel.get("updated_at"),
        }
    except Exception:
        pump_bits = {}
    rhc_bits: dict[str, Any] = {}
    try:
        from desk_realtime.rhc_intel import read_intel as read_rhc

        rhc = read_rhc()
        rhc_bits = {
            "rhc_hot": [str(h.get("symbol") or "") for h in (rhc.get("hot") or [])[:3]],
            "rhc_smart_n": len(rhc.get("smart_money") or []),
            "rhc_tape_n": len(rhc.get("trades") or []),
            "rhc_updated_at": rhc.get("updated_at"),
            "rhc_reason": rhc.get("reason"),
        }
    except Exception:
        rhc_bits = {}
    closed_n = int(_wins) + int(_losses)
    wr = (100.0 * float(_wins) / closed_n) if closed_n else 0.0
    # Live lanes: board + narrative (liquid tape / meme intel) + timing + exit
    agents_live = 2 if valve_closed() or HALT_PATH.exists() else (4 + (1 if _book else 0))
    wsnap = _refresh_wallet_snap()
    write_engine_state({
        "desk_chain": "robinhood",
        "desk_mode": "HALTED" if valve_closed() or HALT_PATH.exists() else "LIVE",
        "valve_gate": "CLOSED" if valve_closed() else "OPEN",
        "slots_open": len(_book),
        "max_slots": max_slots(),
        "wins": _wins,
        "losses": _losses,
        "scouted_count": _scouted,
        "trench": _scouted,
        "not_buy": _skips,
        "net_out": _skips,
        "win_rate": round(wr, 1),
        "agents_live": agents_live,
        "uptime_sec": round(time.time() - boot, 1),
        "realized_net_usdg": float(snap.get("realized_net_usdg") or 0),
        "day_net_usdg": float(snap.get("day_net_usdg") or 0),
        "send_armed": send_armed(),
        "hard_cap": hard_cap(),
        "wallet": (wallet()[:10] + "…") if wallet() else "",
        "wallet_usdg": float(wsnap.get("usdg") or 0.0),
        "wallet_eth": float(wsnap.get("eth") or 0.0),
        "block": int(wsnap.get("block") or 0),
        "wallet_ts": float(wsnap.get("ts") or 0.0),
        "open_book": [
            {
                "symbol": p.get("symbol"),
                "token_address": p.get("token_address"),
                "entry_usdg": p.get("entry_usdg"),
                "opened_at": p.get("opened_at"),
                "live_mult": p.get("live_mult"),
            }
            for p in _book
        ],
        **pump_bits,
        **rhc_bits,
    })
    _persist_book()


def _persist_book() -> None:
    rows = []
    for pos in _book:
        if pos.get("paper"):
            continue
        rows.append({
            "symbol": pos.get("symbol"),
            "token_address": pos.get("token_address"),
            "entry_usdg": pos.get("entry_usdg"),
            "token_amount": pos.get("token_amount"),
            "opened_at": pos.get("opened_at"),
            "live_mult": pos.get("live_mult"),
            "tx_id": pos.get("tx_id"),
            "ledger_id": pos.get("ledger_id") or pos.get("tx_id"),
            "sell_chain_fails": int(pos.get("sell_chain_fails") or 0),
            "sell_retry_until": float(pos.get("sell_retry_until") or 0),
        })
    try:
        _BOOK_PATH.write_text(json.dumps(rows), encoding="utf-8")
    except OSError:
        pass


def _restore_book() -> None:
    global _book
    if not _BOOK_PATH.is_file():
        return
    try:
        raw = json.loads(_BOOK_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, list):
        return
    restored = []
    for row in raw:
        if not isinstance(row, dict) or not row.get("token_address"):
            continue
        item = dict(row)
        item["paper"] = False
        restored.append(item)
    if restored and not _book:
        _book = restored


async def _scan_board(bus: DeskPublisher) -> None:
    global _scouted
    board = await asyncio.to_thread(liquid_board, 12)
    _scouted += max(1, len(board))
    bits = []
    for row in board[:5]:
        bits.append(
            f"{row.get('symbol')} m5={float(row.get('chg_m5') or 0):+.1f}% "
            f"m15={float(row.get('chg_m15') or 0):+.1f}%"
        )
    await bus.emit(
        status="SCAN",
        agent_type="SCANNER",
        token_name="$BOARD",
        entry_size=0,
        log_text="rh uni-v3 · " + (" · ".join(bits) if bits else "empty"),
    )


async def _scan_av_sentiment(bus: DeskPublisher) -> None:
    """Alpha Vantage NEWS_SENTIMENT — TradFi emotion broadcast only. Never buys."""
    try:
        from desk_realtime.alpha_vantage_intel import (
            enabled,
            api_key,
            refresh_universe,
            theme_line,
        )
        from desk_realtime.rh_strategy import active_seed

        if not enabled():
            return
        if not api_key():
            await bus.emit(
                status="SCAN",
                agent_type="NARRATIVE",
                token_name="$AV",
                entry_size=0,
                log_text="av intel · ALPHA_VANTAGE_API_KEY unread · broadcast only",
            )
            return
        syms = [s for s, _ in active_seed()]
        st = await asyncio.to_thread(refresh_universe, syms)
        line = await asyncio.to_thread(theme_line, syms)
        await bus.emit(
            status="SCAN",
            agent_type="NARRATIVE",
            token_name="$AV",
            entry_size=0,
            log_text=f"{line} · intel-only · no auto fill",
        )
        rows = st.get("rows") or []
        hot = [r for r in rows if isinstance(r, dict) and r.get("ok") and abs(float(r.get("score") or 0)) >= 0.2]
        hot.sort(key=lambda r: abs(float(r.get("score") or 0)), reverse=True)
        if hot:
            h = hot[0]
            await bus.emit(
                status="SCAN",
                agent_type="NARRATIVE",
                token_name=f"${str(h.get('symbol') or 'AV')[:8]}",
                entry_size=0,
                log_text=(
                    f"av tone {h.get('tone')} {float(h.get('score') or 0):+.2f} · "
                    f"{str(h.get('headline') or '')[:80]} · not a buy signal"
                ),
            )
    except Exception as exc:  # noqa: BLE001
        await bus.emit(
            status="SCAN",
            agent_type="NARRATIVE",
            token_name="$AV",
            entry_size=0,
            log_text=f"av intel unread · {sanitize_exc(exc)}",
        )


async def _scan_liquid_narrative(bus: DeskPublisher) -> None:
    """NARRATIVE lane for liquid swing: high-beta stock tokens only."""
    board = await asyncio.to_thread(liquid_board, 12)
    sess = session_label()
    label = liquid_universe_label()
    if not board:
        await bus.emit(
            status="SCAN",
            agent_type="NARRATIVE",
            token_name="$SWING",
            entry_size=0,
            log_text=f"liquid · {sess} · {label} · board empty",
        )
        return
    bits = []
    lead = None
    lead_score = -1e9
    for row in board[:4]:
        sym = str(row.get("symbol") or "?")
        sw = row.get("swing") if isinstance(row.get("swing"), dict) else {}
        prem = row.get("premium_pct")
        if prem is None and sw.get("ok"):
            from desk_realtime.rh_strategy import token_premium_pct

            prem, _ = token_premium_pct(float(sw.get("px") or 0), sym)
            row["premium_pct"] = round(prem, 2)
        ta = row.get("equity_ta") if isinstance(row.get("equity_ta"), dict) else None
        if not ta or not ta.get("ok"):
            ta = await asyncio.to_thread(underlying_daily_ta, sym)
            row["equity_ta"] = ta
        if sw.get("ok"):
            rsi = float(sw.get("rsi_1h") or 50)
            prem_s = f" prem={float(prem):+.1f}%" if prem is not None else ""
            macd_s = ""
            if ta and ta.get("ok"):
                macd_s = f" MACDh={float(ta.get('hist') or 0):+.2f}"
            bits.append(f"{sym} RSI={rsi:.0f}{prem_s}{macd_s}")
            score = float(sw.get("px") or 0)
        else:
            bits.append(f"{sym} tape?")
            score = -1e9
        if score > lead_score:
            lead_score = score
            lead = sym
    if session_swing_enabled():
        exit_bits = (
            f"exit RSI≥{rsi_exit_level():.0f} / EMA55-{ema_stop_pct():.1f}% · "
            f"prem≤{max_premium_pct():.0f}% · book≤{max_book_usdg():.1f}U"
            + (" · equity MACD/ATR on" if equity_ta_filter_enabled() else "")
            + " · RHJ gate"
        )
    else:
        exit_bits = f"TP {take_profit():.2f} / stop {hard_stop():.2f}"
    await bus.emit(
        status="SCAN",
        agent_type="NARRATIVE",
        token_name=f"${lead or 'SWING'}",
        entry_size=0,
        log_text=f"liquid · {sess} · {' · '.join(bits)} · {exit_bits}",
    )


async def _scan_pump_intel(bus: DeskPublisher) -> None:
    """Surface Solana pump themes on the desk feed. Never a buy trigger. Meme mode only."""
    try:
        from desk_realtime.pump_intel import theme_line, read_intel

        line = await asyncio.to_thread(theme_line)
        st = await asyncio.to_thread(read_intel)
        await bus.emit(
            status="SCAN",
            agent_type="NARRATIVE",
            token_name="$PUMP",
            entry_size=0,
            log_text=f"{line} · intel-only · no solana fill",
        )
        hot = (st.get("hot") or [])[:1]
        if hot:
            h = hot[0]
            await bus.emit(
                status="SCAN",
                agent_type="NARRATIVE",
                token_name=f"${str(h.get('symbol') or 'HOT')[:12]}",
                entry_size=0,
                log_text=(
                    f"pump hot · theme {h.get('theme')} · "
                    f"meme {float(h.get('meme_score') or 0):.2f} · "
                    f"not a RH buy signal"
                ),
            )
    except Exception as exc:  # noqa: BLE001
        await bus.emit(
            status="SCAN",
            agent_type="NARRATIVE",
            token_name="$PUMP",
            entry_size=0,
            log_text=f"pump intel unread · {sanitize_exc(exc)}",
        )


async def _scan_rhc_intel(bus: DeskPublisher) -> None:
    """RH Chain KOL / smart-money / Uni tape. Read-only — meme mode only."""
    try:
        from desk_realtime.rhc_intel import refresh, theme_line

        # Cap wait so a slow MadeOnSol/PRO 403 cannot stall the loop.
        st = await asyncio.wait_for(asyncio.to_thread(refresh), timeout=12.0)
        line = theme_line()
        await bus.emit(
            status="SCAN",
            agent_type="NARRATIVE",
            token_name="$RHC",
            entry_size=0,
            log_text=f"{line} · intel-only · no auto fill",
        )
        hot = (st.get("hot") or [])[:1]
        if hot:
            h = hot[0]
            await bus.emit(
                status="SCAN",
                agent_type="NARRATIVE",
                token_name=f"${str(h.get('symbol') or 'RHC')[:12]}",
                entry_size=0,
                log_text=(
                    f"rhc hot · kol {int(h.get('kol_count') or 0)} · "
                    f"net {float(h.get('net_eth') or h.get('volume_eth') or 0):.3f} ETH · "
                    f"not a buy signal"
                ),
            )
        sm = st.get("smart_money") or []
        if sm:
            top = sm[0]
            await bus.emit(
                status="SCAN",
                agent_type="RISK",
                token_name="$SMART",
                entry_size=0,
                log_text=(
                    f"smart money · {top.get('wallet')} · "
                    f"net {float(top.get('net_eth') or 0):+.3f} ETH · "
                    f"wr {float(top.get('win_rate') or 0):.0%} · watch only"
                ),
            )
    except asyncio.TimeoutError:
        await bus.emit(
            status="SCAN",
            agent_type="NARRATIVE",
            token_name="$RHC",
            entry_size=0,
            log_text="rhc intel timeout · using cache · no auto fill",
        )
    except Exception as exc:  # noqa: BLE001
        await bus.emit(
            status="SCAN",
            agent_type="NARRATIVE",
            token_name="$RHC",
            entry_size=0,
            log_text=f"rhc intel unread · {sanitize_exc(exc)}",
        )


async def _try_entry(bus: DeskPublisher) -> None:
    global _skips, net_out
    if len(_book) >= max_slots():
        return
    halt, why = day_loss_halt()
    if halt:
        _skips += 1
        net_out = _skips
        await bus.emit(
            status="HOLD_OFF",
            agent_type="RISK",
            token_name="$DESK",
            entry_size=0,
            log_text=f"DAY HALT · {why}",
        )
        set_valve(True, "strategy")
        return

    target = await asyncio.to_thread(pick_short_target, held_addresses() | cooled_addresses())
    if not target:
        _skips += 1
        net_out = _skips
        await bus.emit(
            status="SCAN",
            agent_type="SCANNER",
            token_name="$BOARD",
            entry_size=0,
            log_text=f"early quiet · {quiet_reason()}",
        )
        return

    addr = str(target.get("address") or "")
    sym = str(target.get("symbol") or "?")
    block = trade_block(target)
    if block:
        _skips += 1
        net_out = _skips
        await bus.emit(
            status="VETO",
            agent_type="RISK",
            token_name=f"${sym}",
            entry_size=hard_cap(),
            log_text=f"VETO · {block}",
        )
        return
    if _cooled(addr):
        _skips += 1
        net_out = _skips
        return

    size = hard_cap()
    open_notional = sum(float(p.get("entry_usdg") or 0) for p in _book)
    book_code, book_why = portfolio_block(open_notional=open_notional, next_size=size)
    if book_code:
        _skips += 1
        net_out = _skips
        await bus.emit(
            status="VETO",
            agent_type="RISK",
            token_name=f"${sym}",
            entry_size=size,
            log_text=f"VETO · {format_audit(book_code, book_why)}",
        )
        return
    try:
        from desk_realtime.arc_goplus import scan_token

        gp = await scan_token(addr, symbol=sym)
    except Exception as exc:  # noqa: BLE001
        gp = {
            "buy": False,
            "honeypot": False,
            "provider_na": False,
            "line": f"GOPLUS · scan failed · {sanitize_exc(exc)[:80]}",
        }
    if gp.get("honeypot") or (not gp.get("buy") and not gp.get("provider_na")):
        _skips += 1
        net_out = _skips
        await bus.emit(
            status="VETO",
            agent_type="RISK",
            token_name=f"${sym}",
            entry_size=size,
            log_text=f"VETO · {format_audit('GOPLUS', gp.get('line') or 'GOPLUS · buy blocked')}",
        )
        return
    if gp.get("provider_na"):
        await bus.emit(
            status="VOTING",
            agent_type="RISK",
            token_name=f"${sym}",
            entry_size=size,
            log_text="GOPLUS n/a · roundtrip sell check still on",
        )

    quote = await asyncio.to_thread(quote_roundtrip, addr, usdg_in=size)
    if not quote.get("ok"):
        _skips += 1
        net_out = _skips
        await bus.emit(
            status="VETO",
            agent_type="TIMING",
            token_name=f"${sym}",
            entry_size=size,
            log_text=f"VETO · {format_audit('QUOTE', quote.get('reason') or 'quote fail')}",
        )
        return

    sw = target.get("swing") if isinstance(target.get("swing"), dict) else {}
    if session_swing_enabled() and sw.get("ok"):
        vote_line = (
            f"{session_label()} · RSI {float(sw.get('rsi_1h') or 0):.0f} · "
            f"4hEMA {float(sw.get('ema21_4h') or 0):.2f}/{float(sw.get('ema55_4h') or 0):.2f} · "
            f"quote {quote.get('usdg_in')}→{quote.get('usdg_out')}"
        )
    else:
        vote_line = (
            f"short-term · m5 {float(target.get('chg_m5') or 0):+.2f}% · "
            f"m15 {float(target.get('chg_m15') or 0):+.2f}% · "
            f"quote {quote.get('usdg_in')}→{quote.get('usdg_out')}"
        )
    await bus.emit(
        status="VOTING",
        agent_type="CONSENSUS",
        token_name=f"${sym}",
        entry_size=size,
        log_text=vote_line,
    )

    dry = not send_armed()
    tx_id = ""
    token_amt = int(quote.get("tokens_out") or 0)
    if dry:
        await bus.emit(
            status="BUY",
            agent_type="TIMING",
            token_name=f"${sym}",
            entry_size=size,
            log_text=f"BUY PAPER {sym} · {size:.4f} USDG · RH_UNI_SEND off",
            mult=1.0,
        )
    else:
        fill = await asyncio.to_thread(execute_buy, addr, usdg_in=size)
        tx_id = str(fill.get("tx_id") or "")
        if not fill.get("ok"):
            _cool_set(addr, 600)
            buy_err = str(fill.get("error") or "")
            alert_key = _ops_alert_key(buy_err)
            if alert_key:
                await raise_ops_alert(
                    bus,
                    key=alert_key,
                    title="ETH gas empty" if alert_key == "gas_eth" else "buy blocked",
                    detail=f"${sym} buy failed · {buy_err}",
                    token_name=f"${sym}",
                )
                if alert_key == "gas_eth":
                    await _maybe_refuel_gas(bus, force=True)
            fail_gas = float(fill.get("gas_eth") or 0)
            if fail_gas > 0 and tx_id:
                note_gas(
                    symbol=sym,
                    token=addr,
                    reason=f"buy fail · {buy_err}",
                    tx_id=tx_id,
                    gas_eth=fail_gas,
                    side_hint="buy_fail",
                )
            await bus.emit(
                status="VETO",
                agent_type="TIMING",
                token_name=f"${sym}",
                entry_size=size,
                log_text=f"VETO · buy failed · {buy_err}",
            )
            return
        rec = await asyncio.to_thread(wait_receipt, tx_id)
        gas = float(rec.get("gas_eth") or 0)
        if rec.get("pending") or not rec.get("ok"):
            _cool_set(addr, 900)
            why = rec.get("error") or "reverted"
            if gas > 0 and tx_id:
                note_gas(
                    symbol=sym,
                    token=addr,
                    reason=f"buy {why}",
                    tx_id=tx_id,
                    gas_eth=gas,
                    side_hint="buy_fail",
                )
            await bus.emit(
                status="VETO",
                agent_type="TIMING",
                token_name=f"${sym}",
                entry_size=size,
                log_text=f"VETO · {why} · gas {gas:.6f} ETH · tx {tx_id}",
            )
            return
        token_amt = 0
        read_ok = False
        for _ in range(4):
            read_ok, token_amt = await asyncio.to_thread(erc20_balance_read, addr)
            if read_ok and token_amt > 0:
                break
            await asyncio.sleep(2.0)
        if read_ok and token_amt <= 0:
            _cool_set(addr, 900)
            await bus.emit(
                status="VETO",
                agent_type="TIMING",
                token_name=f"${sym}",
                entry_size=size,
                log_text=f"VETO · receipt ok but balance 0 · tx {tx_id}",
            )
            return
        if not read_ok:
            token_amt = int(quote.get("tokens_out") or 0)
        await bus.emit(
            status="BUY",
            agent_type="TIMING",
            token_name=f"${sym}",
            entry_size=size,
            log_text=f"BUY {sym} · {size:.4f} USDG · tx {tx_id}",
            mult=1.0,
        )

    trade_id = tx_id.lower() if tx_id else ""
    if not dry and tx_id:
        trade_id = note_buy(
            symbol=sym,
            token=addr,
            usdg_in=size,
            token_amount=token_amt,
            tx_id=tx_id,
            gas_eth=float(fill.get("gas_eth") or gas or 0),
        )
    _book.append({
        "symbol": sym,
        "token_address": addr.lower(),
        "entry_usdg": size,
        "token_amount": token_amt,
        "opened_at": time.time(),
        "live_mult": 1.0,
        "tx_id": tx_id,
        "ledger_id": trade_id,
        "paper": dry,
    })


async def _run_exit(bus: DeskPublisher, pos: dict[str, Any], *, force: bool = False, reason: str = "") -> None:
    global _wins, _losses, net_out
    sym = str(pos.get("symbol") or "?")
    addr = str(pos.get("token_address") or "")
    entry = float(pos.get("entry_usdg") or hard_cap())
    amt = int(pos.get("token_amount") or 0)
    held = time.time() - float(pos.get("opened_at") or time.time())
    chain_amt = int(pos.get("token_amount") or 0)
    if not pos.get("paper"):
        read_ok, chain_amt = await asyncio.to_thread(erc20_balance_read, addr)
        if not read_ok:
            await bus.emit(
                status="HOLD",
                agent_type="EXIT",
                token_name=f"${sym}",
                entry_size=entry,
                log_text=f"mark unread · held {held:.0f}s · keep",
                mult=float(pos.get("live_mult") or 0),
            )
            return
    if not pos.get("paper") and chain_amt <= 0:
        await bus.emit(
            status="EXIT",
            agent_type="EXIT",
            token_name=f"${sym}",
            entry_size=entry,
            log_text="FLAT · no token balance · no sell · gas unread",
            mult=0.0,
        )
        _cool_set(addr, reentry_sec())
        _book[:] = [p for p in _book if p is not pos]
        return

    mark = await asyncio.to_thread(
        quote_held_mark, addr, token_amount=chain_amt or amt, cost_usdg=entry
    )
    live = float(mark.get("live_mult") or 0.0)
    if live <= 0 and pos.get("paper"):
        # Paper mark: drift from board m5 as a stand-in.
        live = 1.0
        for row in liquid_board():
            if str(row.get("address") or "").lower() == addr.lower():
                live = max(0.5, 1.0 + float(row.get("chg_m5") or 0) / 100.0)
                break
    pos["live_mult"] = live

    fire, why = should_exit(live_mult=live, held_sec=held, token=addr)
    if force:
        fire, why = True, reason or "PANIC"
    if not fire:
        await bus.emit(
            status="HOLD",
            agent_type="EXIT",
            token_name=f"${sym}",
            entry_size=entry,
            log_text=f"mark {live:.3f}x · held {held:.0f}s",
            mult=live,
            mark_usdc=float(mark.get("mark_usdg") or 0),
        )
        return

    if pos.get("paper") or not send_armed():
        sell_ok = True
        err = ""
        sell_tx = ""
        sell: dict[str, Any] = {}
    else:
        cool_until = float(pos.get("sell_retry_until") or 0.0)
        chain_fails = int(pos.get("sell_chain_fails") or 0)
        if not force and cool_until > time.time():
            left = cool_until - time.time()
            await bus.emit(
                status="HOLD",
                agent_type="EXIT",
                token_name=f"${sym}",
                entry_size=entry,
                log_text=f"sell cool {left:.0f}s · fails {chain_fails}/{sell_fail_max_chain()}",
                mult=live,
            )
            return
        # Cool expired after a fail streak → reset and retry with a fresh quote.
        # Without this, sell_chain_fails stays ≥ max and the slot is stuck forever.
        if not force and chain_fails >= sell_fail_max_chain():
            pos["sell_chain_fails"] = 0
            pos["sell_retry_until"] = 0.0
            chain_fails = 0
            try:
                _persist_book()
            except Exception:
                pass
            await bus.emit(
                status="HOLD",
                agent_type="EXIT",
                token_name=f"${sym}",
                entry_size=entry,
                log_text="sell retry · fail streak reset · fresh quote",
                mult=live,
            )
        sell_amt = int(chain_amt or amt)
        sell = await asyncio.to_thread(execute_sell, addr, token_amount=sell_amt)
        sell_ok = bool(sell.get("ok"))
        err = str(sell.get("error") or "")
        sell_tx = str(sell.get("tx_id") or "")

    if not sell_ok:
        alert_key = _ops_alert_key(err)
        if alert_key:
            await raise_ops_alert(
                bus,
                key=alert_key,
                title="ETH gas empty" if alert_key == "gas_eth" else "sell blocked",
                detail=f"${sym} stuck · {err}",
                token_name=f"${sym}",
            )
            if alert_key == "gas_eth":
                await _maybe_refuel_gas(bus, force=True)
        fail_gas = float(sell.get("gas_eth") or 0) if not pos.get("paper") else 0.0
        if fail_gas <= 0 and "gas " in err.lower():
            try:
                # "… gas 0.000012 ETH · tx …"
                part = err.lower().split("gas ", 1)[1]
                fail_gas = float(part.split(" eth", 1)[0].strip())
            except Exception:
                fail_gas = 0.0
        if fail_gas > 0 and sell_tx:
            note_gas(
                symbol=sym,
                token=addr,
                reason=f"{why} · {err}",
                tx_id=sell_tx,
                gas_eth=fail_gas,
                side_hint="sell_fail",
            )
        # Back off so the 2s exit loop cannot spam failed broadcasts.
        if not pos.get("paper") and send_armed():
            if sell.get("sim_blocked") or not sell_tx:
                pos["sell_retry_until"] = time.time() + min(20.0, sell_fail_cooldown_sec())
            else:
                fails = int(pos.get("sell_chain_fails") or 0) + 1
                pos["sell_chain_fails"] = fails
                # After N chain reverts, cool longer then auto-reset (see above).
                if fails >= sell_fail_max_chain():
                    pos["sell_retry_until"] = time.time() + max(
                        180.0, sell_fail_cooldown_sec() * 4
                    )
                else:
                    pos["sell_retry_until"] = time.time() + sell_fail_cooldown_sec()
            try:
                _persist_book()
            except Exception:
                pass
        await bus.emit(
            status="EXIT",
            agent_type="EXIT",
            token_name=f"${sym}",
            entry_size=entry,
            log_text=f"ERROR sell · {err}",
            mult=live,
        )
        return

    if not pos.get("paper"):
        left_ok, left = await asyncio.to_thread(erc20_balance_read, addr)
        if not left_ok:
            await bus.emit(
                status="HOLD",
                agent_type="EXIT",
                token_name=f"${sym}",
                entry_size=entry,
                log_text=f"sell receipt ok · balance unread · keep · tx {sell_tx}",
                mult=live,
            )
            return
        if left > max(1, int((chain_amt or amt) * 0.05)):
            pos["token_amount"] = left
            await bus.emit(
                status="HOLD",
                agent_type="EXIT",
                token_name=f"${sym}",
                entry_size=entry,
                log_text=f"sell left {left} · keep · tx {sell_tx}",
                mult=live,
            )
            return

    snap = record_close_pnl(entry_usdg=entry, exit_mult=live if live > 0 else 1.0)
    usdg_out = float(entry) * (float(live) if live > 0 else 1.0)
    if sell_tx:
        got = await asyncio.to_thread(usdg_flow, sell_tx)
        if got is not None and got > 0:
            usdg_out = got
    if not pos.get("paper"):
        note_sell(
            trade_id=str(pos.get("ledger_id") or pos.get("tx_id") or ""),
            symbol=sym,
            token=addr,
            reason=why,
            entry_usdg=entry,
            usdg_out=usdg_out,
            token_amount=int(chain_amt or amt),
            mult=live,
            tx_id=sell_tx,
            gas_eth=float(sell.get("gas_eth") or 0) if not pos.get("paper") and send_armed() else 0.0,
        )
    net = usdg_out - float(entry)
    if net >= 0:
        _wins += 1
    else:
        _losses += 1
    await bus.emit(
        status="EXIT",
        agent_type="EXIT",
        token_name=f"${sym}",
        entry_size=entry,
        log_text=f"{why} · {live:.3f}x · net {net:+.3f} · day {float(snap.get('day_net_usdg') or 0):+.3f}",
        mult=live,
    )
    _cool = float(reentry_sec())
    if why == "HARD STOP":
        _cool = max(_cool, _cool * 2.0, 900.0)
    _cool_set(addr, _cool)
    _book[:] = [p for p in _book if p is not pos]


async def _panic_flatten(bus: DeskPublisher) -> None:
    for pos in list(_book):
        await _run_exit(bus, pos, force=True, reason="PANIC SELL")
    set_valve(True, "panic")
    await bus.emit(
        status="HOLD_OFF",
        agent_type="EXIT",
        token_name="$DESK",
        entry_size=0,
        log_text="VALVE CLOSED · panic flatten",
    )


def reconcile_enabled() -> bool:
    return (os.environ.get("RH_RECONCILE") or "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def reconcile_every_sec() -> float:
    try:
        return max(30.0, float(os.environ.get("RH_RECONCILE_SEC", "120")))
    except ValueError:
        return 120.0


async def _reconcile_book(bus: DeskPublisher) -> None:
    """Light book↔chain check. Clear flat bags; sync token_amount drift."""
    if not reconcile_enabled() or not _book:
        return
    changed = False
    for pos in list(_book):
        if pos.get("paper"):
            continue
        sym = str(pos.get("symbol") or "?")
        addr = str(pos.get("token_address") or "")
        book_amt = int(pos.get("token_amount") or 0)
        entry = float(pos.get("entry_usdg") or 0)
        read_ok, chain_amt = await asyncio.to_thread(erc20_balance_read, addr)
        if not read_ok:
            continue
        if chain_amt <= 0:
            await bus.emit(
                status="EXIT",
                agent_type="EXIT",
                token_name=f"${sym}",
                entry_size=entry,
                log_text=format_audit("RECON", "book flat on chain · cleared"),
                mult=0.0,
            )
            _cool_set(addr, reentry_sec())
            _book[:] = [p for p in _book if p is not pos]
            changed = True
            continue
        if book_amt > 0 and abs(chain_amt - book_amt) / max(book_amt, 1) > 0.05:
            pos["token_amount"] = chain_amt
            changed = True
            await bus.emit(
                status="SCAN",
                agent_type="RISK",
                token_name=f"${sym}",
                entry_size=entry,
                log_text=format_audit(
                    "RECON",
                    f"amt sync {book_amt}→{chain_amt}",
                ),
            )
    if changed:
        _persist_book()


async def rh_main_loop(
    ws_url: str,
    *,
    scan_every: float = 8.0,
    exit_every: float = 2.0,
) -> None:
    bus = DeskPublisher(ws_url)
    await bus.ensure_connected()
    boot = time.time()
    _restore_book()
    _restore_cool()
    pf = await asyncio.to_thread(preflight)
    journal_day = time.strftime("%Y-%m-%d", time.localtime())
    last_board = 0.0
    last_pump = 0.0
    last_rhc = 0.0
    last_av = 0.0
    last_recon = 0.0

    rhj_on = (os.environ.get("RH_RHJ_MARKET") or "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    await bus.emit(
        status="INIT",
        agent_type="EXIT",
        token_name="$DESK",
        entry_size=hard_cap(),
        log_text=(
            f"rh loop armed · chain {pf.get('chain_id')} · "
            f"{'rpc ok' if pf.get('ok') else 'rpc degraded'} · "
            f"universe {universe_mode()} · "
            f"session {session_label()} · "
            f"cap {hard_cap():.2f} USDG · book≤{max_book_usdg():.1f} · "
            f"stop {hard_stop():.2f} · "
            f"{'hold OFF · TP/stop only' if max_hold_sec() <= 0 else f'hold {int(max_hold_sec())}s'} · "
            f"send {'ON' if send_armed() else 'OFF'} · "
            f"gas-refuel {'ON' if gas_refuel_enabled() else 'OFF'}@{gas_refuel_usdg():.1f}U · "
            + (
                (
                    f"session-swing · windows pre/RTH/power · "
                    f"intraday {'ON' if intraday_enabled() else 'OFF'} · "
                    f"prem≤{max_premium_pct():.0f}% · "
                    f"RHJ {'ON' if rhj_on else 'OFF'} · "
                    f"equityTA {'ON' if equity_ta_filter_enabled() else 'OFF'} · "
                    f"AV-news broadcast · "
                    f"{liquid_universe_label()}"
                )
                if session_swing_enabled()
                else (
                    f"narrative · {liquid_universe_label()}"
                    if universe_mode() not in ("meme", "trench", "legacy")
                    else "pump+rhc intel read-only"
                )
            )
        ),
    )
    _write_state(boot=boot)

    try:
        while True:
            today = time.strftime("%Y-%m-%d", time.localtime())
            if today != journal_day:
                prev = journal_day
                journal_day = today
                try:
                    from desk_realtime.daily_journal import write_journal

                    path = await asyncio.to_thread(write_journal, prev)
                    await bus.emit(
                        status="SCAN",
                        agent_type="SCANNER",
                        token_name="$JOURNAL",
                        entry_size=0,
                        log_text=f"journal {prev} · {path.name}",
                    )
                except Exception:
                    journal_day = prev

            panic = consume_panic()
            if panic is not None:
                await _panic_flatten(bus)
                _write_state(boot=boot)
                await asyncio.sleep(0.5)
                continue

            await _maybe_refuel_gas(bus)

            now = time.time()
            if now - last_board >= 45:
                last_board = now
                await _scan_board(bus)
            meme_intel = universe_mode() in ("meme", "trench", "legacy")
            if meme_intel:
                if now - last_pump >= 60:
                    last_pump = now
                    await _scan_pump_intel(bus)
                if now - last_rhc >= 60:
                    last_rhc = now
                    await _scan_rhc_intel(bus)
            elif now - last_pump >= 60:
                last_pump = now
                await _scan_liquid_narrative(bus)
            if (
                universe_mode() not in ("meme", "trench", "legacy")
                and now - last_av >= 300
            ):
                last_av = now
                await _scan_av_sentiment(bus)

            for pos in list(_book):
                await _run_exit(bus, pos)

            if now - last_recon >= reconcile_every_sec():
                last_recon = now
                await _reconcile_book(bus)

            if valve_closed() or HALT_PATH.exists():
                if not _book:
                    await bus.emit(
                        status="HOLD_OFF",
                        agent_type="EXIT",
                        token_name="$DESK",
                        entry_size=hard_cap(),
                        log_text="VALVE CLOSED · no new buys · book flat",
                    )
                _write_state(boot=boot)
                await asyncio.sleep(exit_every)
                continue

            if len(_book) < max_slots():
                await _try_entry(bus)
                _write_state(boot=boot)
                await asyncio.sleep(scan_every if not _book else exit_every)
            else:
                _write_state(boot=boot)
                await asyncio.sleep(exit_every)
    finally:
        await bus.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    os.environ.setdefault("DESK_CHAIN", "robinhood")
    p = argparse.ArgumentParser(description="Robinhood Chain short-term loop → desk WS")
    p.add_argument("--ws", default="ws://127.0.0.1:8765")
    p.add_argument("--scan-every", type=float, default=8.0)
    p.add_argument("--exit-every", type=float, default=2.0)
    args = p.parse_args()
    asyncio.run(rh_main_loop(args.ws, scan_every=args.scan_every, exit_every=args.exit_every))


if __name__ == "__main__":
    main()
