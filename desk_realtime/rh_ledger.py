"""One line per Robinhood fill, plus a same-day summary.

Tx hashes stay in this file. The desk feed redacts 32-byte hex, so the
loop log cannot be the book of record.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_LEDGER = _ROOT / "grok-trading-desk" / "logs" / "rh_trades.jsonl"
_DIR = _ROOT / "grok-trading-desk" / "logs" / "journal"


def _now_local() -> datetime:
    return datetime.now().astimezone()


def _append(row: dict[str, Any]) -> None:
    _LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with _LEDGER.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _load() -> list[dict[str, Any]]:
    if not _LEDGER.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in _LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def usdg_flow(tx_id: str) -> float | None:
    """Net USDG into the desk wallet for one transaction. None if unread."""
    hx = str(tx_id or "")
    if not (hx.startswith("0x") and len(hx) >= 66):
        return None
    try:
        from desk_realtime.rh_net import USDG, rpc_json, wallet

        raw = rpc_json("eth_getTransactionReceipt", [hx], timeout=8)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    who = wallet().lower()
    sig = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    net = 0
    for item in raw.get("logs") or []:
        if str(item.get("address") or "").lower() != USDG.lower():
            continue
        topics = [str(t).lower() for t in (item.get("topics") or [])]
        if len(topics) < 3 or topics[0] != sig:
            continue
        amt = int(item.get("data") or "0x0", 16)
        frm = "0x" + topics[1][-40:]
        to = "0x" + topics[2][-40:]
        if to == who:
            net += amt
        if frm == who:
            net -= amt
    return net / 1_000_000


def receipt_gas_eth(tx_id: str) -> float:
    """ETH spent on one tx from the chain receipt. 0 if unread."""
    hx = str(tx_id or "")
    if not (hx.startswith("0x") and len(hx) >= 66):
        return 0.0
    try:
        from desk_realtime.rh_net import tx_receipt

        rec = tx_receipt(hx, timeout=12)
    except Exception:
        return 0.0
    if rec.get("pending"):
        return 0.0
    return float(rec.get("gas_eth") or 0.0)


def _resolve_gas(tx_id: str, gas_eth: float) -> float:
    gas = float(gas_eth or 0)
    if gas > 0:
        return gas
    return receipt_gas_eth(tx_id)


def has_tx(tx_id: str, side: str) -> bool:
    tx = (tx_id or "").lower()
    if not tx:
        return False
    return any(
        str(row.get("tx") or "").lower() == tx and str(row.get("side") or "") == side
        for row in _load()
    )


def note_buy(
    *,
    symbol: str,
    token: str,
    usdg_in: float,
    token_amount: int,
    tx_id: str,
    gas_eth: float = 0.0,
    trade_id: str = "",
    when: datetime | None = None,
) -> str:
    """Record a confirmed buy. Returns the trade id used to close it."""
    now = when or _now_local()
    tid = (trade_id or tx_id or f"{symbol}-{int(time.time())}").lower()
    if has_tx(tx_id, "buy"):
        write_day(now.strftime("%Y-%m-%d"))
        return tid
    gas = _resolve_gas(tx_id, gas_eth)
    _append({
        "day": now.strftime("%Y-%m-%d"),
        "clock": now.strftime("%H:%M:%S"),
        "ts": now.timestamp(),
        "side": "buy",
        "trade_id": tid,
        "symbol": symbol,
        "token": token.lower(),
        "usdg_in": round(float(usdg_in), 6),
        "token_amount": int(token_amount),
        "tx": tx_id,
        "gas_eth": gas,
    })
    write_day(now.strftime("%Y-%m-%d"))
    return tid


def note_sell(
    *,
    trade_id: str,
    symbol: str,
    token: str,
    reason: str,
    entry_usdg: float,
    usdg_out: float,
    token_amount: int,
    mult: float,
    tx_id: str,
    gas_eth: float = 0.0,
    when: datetime | None = None,
) -> None:
    now = when or _now_local()
    if has_tx(tx_id, "sell"):
        write_day(now.strftime("%Y-%m-%d"))
        return
    entry = float(entry_usdg)
    out = float(usdg_out)
    gas = _resolve_gas(tx_id, gas_eth)
    _append({
        "day": now.strftime("%Y-%m-%d"),
        "clock": now.strftime("%H:%M:%S"),
        "ts": now.timestamp(),
        "side": "sell",
        "trade_id": (trade_id or tx_id).lower(),
        "symbol": symbol,
        "token": token.lower(),
        "reason": reason,
        "entry_usdg": round(entry, 6),
        "usdg_out": round(out, 6),
        "net_usdg": round(out - entry, 6),
        "mult": round(float(mult), 4),
        "token_amount": int(token_amount),
        "tx": tx_id,
        "gas_eth": gas,
    })
    write_day(now.strftime("%Y-%m-%d"))


def note_gas(
    *,
    symbol: str,
    token: str,
    reason: str,
    tx_id: str,
    gas_eth: float,
    side_hint: str = "fail",
    when: datetime | None = None,
) -> None:
    """Record ETH burned on a failed or partial send. Does not change USDG PnL."""
    hx = str(tx_id or "")
    gas = _resolve_gas(hx, gas_eth)
    if gas <= 0 or not (hx.startswith("0x") and len(hx) >= 66):
        return
    if has_tx(hx, "gas"):
        return
    now = when or _now_local()
    _append({
        "day": now.strftime("%Y-%m-%d"),
        "clock": now.strftime("%H:%M:%S"),
        "ts": now.timestamp(),
        "side": "gas",
        "kind": side_hint,
        "symbol": symbol,
        "token": (token or "").lower(),
        "reason": reason,
        "tx": hx,
        "gas_eth": gas,
    })
    write_day(now.strftime("%Y-%m-%d"))


def backfill_missing_gas() -> int:
    """Fill gas_eth=0 rows from chain receipts. Rewrites the ledger file."""
    rows = _load()
    changed = 0
    out: list[dict[str, Any]] = []
    for row in rows:
        gas = float(row.get("gas_eth") or 0)
        tx = str(row.get("tx") or "")
        if gas <= 0 and tx.startswith("0x"):
            got = receipt_gas_eth(tx)
            if got > 0:
                row = dict(row)
                row["gas_eth"] = got
                changed += 1
        out.append(row)
    if changed:
        _LEDGER.parent.mkdir(parents=True, exist_ok=True)
        tmp = _LEDGER.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for row in out:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp.replace(_LEDGER)
        days = sorted({str(r.get("day") or "") for r in out if r.get("day")})
        for day in days:
            write_day(day)
    return changed


def write_day(day: str | None = None) -> Path:
    """Rewrite journal/YYYY-MM-DD.md from the fill ledger."""
    day = day or _now_local().strftime("%Y-%m-%d")
    all_rows = _load()
    rows = [row for row in all_rows if row.get("day") == day]
    buys = [row for row in rows if row.get("side") == "buy"]
    sells = [row for row in rows if row.get("side") == "sell"]
    gas_rows = [row for row in rows if row.get("side") == "gas"]
    later_closed = {
        str(row.get("trade_id") or "")
        for row in all_rows
        if row.get("side") == "sell"
    }
    opens = [row for row in buys if str(row.get("trade_id") or "") not in later_closed]
    spent = sum(float(row.get("usdg_in") or 0) for row in buys)
    got = sum(float(row.get("usdg_out") or 0) for row in sells)
    net = sum(float(row.get("net_usdg") or 0) for row in sells)
    wins = sum(1 for row in sells if float(row.get("net_usdg") or 0) >= 0)
    losses = len(sells) - wins
    gas_buy = sum(float(row.get("gas_eth") or 0) for row in buys)
    gas_sell = sum(float(row.get("gas_eth") or 0) for row in sells)
    gas_fail = sum(float(row.get("gas_eth") or 0) for row in gas_rows)
    gas_total = gas_buy + gas_sell + gas_fail

    lines = [
        f"# 交易日志 {day}",
        "",
        "每笔只在成交确认后写入。USDG 盈亏与 ETH gas 分开结算；进出都记 gas。",
        "完整记录在 grok-trading-desk/logs/rh_trades.jsonl。",
        "",
        "## 当日汇总",
        f"- 买入 {len(buys)} 笔 · 花费 {spent:.3f} USDG",
        f"- 平仓 {len(sells)} 笔 · 收回 {got:.3f} USDG",
        f"- 已实现 {net:+.3f} USDG · 胜 {wins} / 负 {losses}",
        f"- 未平 {len(opens)} 笔",
        "",
        "## ETH 手续费结算",
        f"- 买入 gas {gas_buy:.8f} ETH",
        f"- 卖出 gas {gas_sell:.8f} ETH",
        f"- 失败/退回 gas {gas_fail:.8f} ETH",
        f"- 合计 {gas_total:.8f} ETH",
        "",
        "## 已平仓",
    ]
    by_id = {
        str(row.get("trade_id") or ""): row
        for row in all_rows
        if row.get("side") == "buy" and row.get("trade_id")
    }
    if sells:
        for sell in sells:
            buy = by_id.get(str(sell.get("trade_id") or ""))
            bought = buy.get("clock") if buy else "—"
            g_buy = float((buy or {}).get("gas_eth") or 0)
            g_sell = float(sell.get("gas_eth") or 0)
            lines.append(
                "- {buy_at} 买 ${sym} {inn:.3f} → {sell_at} {why} {out:.3f} · {net:+.3f} USDG · {mult:.3f}x".format(
                    buy_at=bought,
                    sym=sell.get("symbol") or "?",
                    inn=float((buy or {}).get("usdg_in") or sell.get("entry_usdg") or 0),
                    sell_at=sell.get("clock") or "",
                    why=sell.get("reason") or "SELL",
                    out=float(sell.get("usdg_out") or 0),
                    net=float(sell.get("net_usdg") or 0),
                    mult=float(sell.get("mult") or 0),
                )
            )
            lines.append(f"  - 买 {buy.get('tx') if buy else '—'}")
            lines.append(f"  - 卖 {sell.get('tx') or '—'}")
            lines.append(f"  - gas 买 {g_buy:.8f} + 卖 {g_sell:.8f} = {g_buy + g_sell:.8f} ETH")
    else:
        lines.append("- 无")
    lines.extend(["", "## 持仓"])
    if opens:
        for buy in opens:
            lines.append(
                f"- {buy.get('clock')} 买 ${buy.get('symbol')} {float(buy.get('usdg_in') or 0):.3f} USDG · 未平"
                f" · gas {float(buy.get('gas_eth') or 0):.8f} ETH"
            )
            lines.append(f"  - {buy.get('tx') or '—'}")
    else:
        lines.append("- 无")
    lines.extend(["", "## ETH 失败单"])
    if gas_rows:
        for row in gas_rows:
            lines.append(
                f"- {row.get('clock')} ${row.get('symbol') or '?'} {row.get('reason') or row.get('kind')}"
                f" · {float(row.get('gas_eth') or 0):.8f} ETH"
            )
            lines.append(f"  - {row.get('tx') or '—'}")
    else:
        lines.append("- 无")
    lines.append("")
    _DIR.mkdir(parents=True, exist_ok=True)
    path = _DIR / f"{day}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
