"""Normalize inbound WS JSON → desk.jsonl-shaped records + panel hints."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from desk_realtime.desk_units import ENTRY, QUOTE, normalize_fill_amount


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _token(ev: dict[str, Any]) -> str:
    raw = (
        ev.get("token_name")
        or ev.get("token")
        or ev.get("symbol")
        or "TOKEN"
    )
    return str(raw).lstrip("$").upper()


def _agent(ev: dict[str, Any], default: str = "") -> str:
    return str(ev.get("agent_type") or ev.get("agent") or default).upper()


def _status(ev: dict[str, Any]) -> str:
    return str(ev.get("status") or ev.get("type") or ev.get("event") or "").upper()


def _amount(ev: dict[str, Any]) -> float:
    try:
        raw = float(ev.get("entry_size") or ev.get("amount") or ENTRY)
    except (TypeError, ValueError):
        raw = ENTRY
    return normalize_fill_amount(raw)


def _log_text(ev: dict[str, Any], fallback: str) -> str:
    return str(ev.get("log_text") or ev.get("msg") or ev.get("note") or fallback)


def _buy_detail(ev: dict[str, Any], *, panel: str, agent: str, note: str) -> dict[str, Any]:
    """Keep the loop's live mark. Do not invent a multiple here."""
    detail: dict[str, Any] = {
        "event": "ENTRY" if panel == "ARMING" else "BUY",
        "agent_type": agent or "TIMING",
        "panel": panel,
        "note": note,
        "source": "ws",
        "unit": QUOTE,
    }
    raw_mult = ev.get("mult") if ev.get("mult") is not None else ev.get("multiple")
    if raw_mult is not None:
        try:
            detail["mult"] = float(raw_mult)
        except (TypeError, ValueError):
            pass
    try:
        mark_usdc = float(ev.get("mark_usdc") or 0)
    except (TypeError, ValueError):
        mark_usdc = 0.0
    if mark_usdc > 0:
        detail["mark_usdc"] = mark_usdc
    if ev.get("mark_src"):
        detail["mark_src"] = str(ev.get("mark_src"))
    return detail


def normalize_ws_event(ev: dict[str, Any]) -> dict[str, Any] | None:
    """
    Map a live WS packet into one jsonl row the existing ingest understands.

    Accepted shapes (any alias works):
      {timestamp, agent_type, status, token_name, entry_size, log_text}
      {type/status: VETO|VOTING|BUY|EXIT|INIT, agent, token, ...}
    """
    if not isinstance(ev, dict):
        return None
    # Control frames
    kind = str(ev.get("kind") or "").lower()
    if kind in ("ping", "pong", "heartbeat"):
        return None
    if ev.get("op") in ("ping", "pong"):
        return None

    status = _status(ev)
    if not status:
        return None
    # Telemetry-only — handled by DeskBus.set_metrics, not the trade log
    if status == "METRICS" or str(ev.get("op") or "").lower() == "metrics":
        return None

    tok = _token(ev)
    amt = _amount(ev)
    ts = str(ev.get("timestamp") or ev.get("ts") or _now())
    agent = _agent(ev)

    if status in ("INIT", "BOOT", "HOLD_OFF", "HOLD"):
        note = _log_text(ev, "session open · HOLD OFF")
        return {
            "ts": ts,
            "type": "action",
            "symbol": tok if tok != "TOKEN" else "DESK",
            "action": "ARM",
            "reason": note,
            "market": "crypto",
            "detail": {
                "event": "BOOT",
                "agent_type": agent or "EXIT",
                "panel": "HOLD_OFF",
                "note": note,
                "source": "ws",
            },
        }

    if status in ("VOTING", "SCORE", "NARRATIVE"):
        note = _log_text(ev, f"narrative theme match ${tok}")
        return {
            "ts": ts,
            "type": "action",
            "symbol": tok,
            "action": "SCORE",
            "reason": note,
            "market": "crypto",
            "detail": {
                "event": "SCORE",
                "agent_type": agent or "NARRATIVE",
                "panel": "VOTING",
                "note": note,
                "source": "ws",
            },
        }

    if status in ("VETO", "STOPPED_OUT", "STOP", "NOT_BUY", "RISK", "NET_OUT"):
        note = _log_text(ev, "book too thin (risk veto)")
        agent = agent or ("NARRATIVE" if ev.get("off_narrative") else "RISK")
        tier = str(ev.get("audit_tier") or "").upper()
        reason = "off_narrative" if ev.get("off_narrative") else "thin_liquidity"
        if "AUDIT" in note.upper() or "待复核" in note or "拒绝" in note:
            reason = "audit_review" if ("待复核" in note or tier == "REVIEW") else "audit_reject"
            if tier == "REJECT":
                reason = "audit_reject"
        return {
            "ts": ts,
            "type": "skip",
            "market": "crypto",
            "symbol": tok,
            "reason": reason,
            "detail": {
                "bot": "crypto_checker",
                "event": "NOT BUY",
                "agent_type": agent,
                "panel": "STOPPED_OUT",
                "note": note,
                "source": "ws",
                "audit_tier": tier or None,
                "net_out": ev.get("net_out"),
                "net_out_delta": ev.get("net_out_delta", 1),
            },
        }

    if status in ("BUY", "ENTRY", "ARMING", "OPEN"):
        note = _log_text(ev, f"ENTRY ${tok} opened {amt:.2f} {QUOTE}")
        panel = "ARMING" if status in ("ENTRY", "ARMING") else "BUY"
        return {
            "ts": ts,
            "type": "buy",
            "market": "crypto",
            "symbol": tok,
            "score": float(ev.get("score") or 0.88),
            "amount": amt,
            "tx_id": str(ev.get("tx_id") or "ws_live"),
            "token_address": str(ev.get("token_address") or ""),
            "launchpad": str(ev.get("launchpad") or ""),
            "all_agent_scores": ev.get("all_agent_scores")
            or {
                "narrative": {"virality": 0.94},
                "auditor": {"organic_score": 0.82},
                "crypto_pulse": {"go_signal": 0.78},
            },
            "detail": _buy_detail(ev, panel=panel, agent=agent, note=note),
        }

    if status in ("EXIT", "CLOSE", "SETTLE"):
        mult = float(ev.get("mult") or ev.get("multiple") or 4.0)
        note = _log_text(ev, f"exit fired · {mult:.0f}x locked")
        return {
            "ts": ts,
            "type": "close",
            "market": "crypto",
            "symbol": tok,
            "pnl": float(ev.get("pnl") or amt * (mult - 1.0)),
            "hold_time": float(ev.get("hold_time") or 1.2),
            "detail": {
                "event": "EXIT",
                "agent_type": agent or "EXIT",
                "panel": "HOLD_OFF",
                "mult": mult,
                "note": note,
                "source": "ws",
            },
        }

    if status == "SCAN":
        note = _log_text(ev, f"SCAN fresh launch ${tok}")
        return {
            "ts": ts,
            "type": "action",
            "market": "crypto",
            "symbol": tok,
            "action": "SCAN",
            "reason": note,
            "detail": {
                "bot": "scanner",
                "event": "SCAN",
                "agent_type": agent or "SCANNER",
                "note": note,
                "source": "ws",
            },
        }

    # Pass-through already-shaped jsonl rows
    if ev.get("type") in ("buy", "skip", "close", "action", "cost", "allocation"):
        out = dict(ev)
        out.setdefault("ts", ts)
        return out

    return None


def stage_packets(token: str = "ARCMEME") -> list[dict[str, Any]]:
    """Five-stage mock sequence for end-to-end UI verification."""
    tok = token.lstrip("$").upper()
    return [
        {
            "timestamp": _now(),
            "status": "INIT",
            "agent_type": "EXIT",
            "token_name": f"${tok}",
            "entry_size": ENTRY,
            "log_text": "system init · HOLD OFF",
        },
        {
            "timestamp": _now(),
            "status": "VOTING",
            "agent_type": "NARRATIVE",
            "token_name": f"${tok}",
            "entry_size": ENTRY,
            "log_text": f"narrative 0.94 theme match ${tok}",
        },
        {
            "timestamp": _now(),
            "status": "VETO",
            "agent_type": "RISK",
            "token_name": f"${tok}",
            "entry_size": ENTRY,
            "log_text": "book too thin · risk veto",
        },
        {
            "timestamp": _now(),
            "status": "BUY",
            "agent_type": "TIMING",
            "token_name": f"${tok}",
            "entry_size": ENTRY,
            "log_text": f"BUY ${tok} — liquidity doubled, veto gone · {ENTRY:.2f} {QUOTE}",
            "score": 0.91,
        },
        {
            "timestamp": _now(),
            "status": "EXIT",
            "agent_type": "EXIT",
            "token_name": f"${tok}",
            "entry_size": ENTRY,
            "mult": 4.0,
            "pnl": round(ENTRY * 3.0, 4),
            "log_text": f"exit fired · 4x locked · {QUOTE}",
        },
    ]
