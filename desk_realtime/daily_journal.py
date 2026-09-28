"""One local-day trading journal. Facts from the loop log, not a new strategy.

Supports Arc (trading_loop.log / USDC) and Robinhood Chain (rh_loop.log / USDG).
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ARC_LOG = _ROOT / "grok-trading-desk" / "logs" / "trading_loop.log"
_RH_LOG = _ROOT / "grok-trading-desk" / "logs" / "rh_loop.log"
_ARC_PNL = _ROOT / "grok-trading-desk" / "logs" / "realized_pnl.json"
_RH_PNL = _ROOT / "grok-trading-desk" / "logs" / "rh_realized_pnl.json"
_STATE = _ROOT / "grok-trading-desk" / "logs" / "engine_state.json"
_DIR = _ROOT / "grok-trading-desk" / "logs" / "journal"

_LINE = re.compile(
    r"^(?P<day>\d{4}-\d{2}-\d{2}) (?P<clock>\d{2}:\d{2}:\d{2}),\d+ .* "
    r"emit (?P<status>\w+) \$(?P<sym>\S+) \| (?P<note>.*)$"
)
_NET = re.compile(r"net ([+-]?\d+(?:\.\d+)?)")
_MULT = re.compile(r"(\d+(?:\.\d+)?)x")


def yesterday(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return (now - timedelta(days=1)).strftime("%Y-%m-%d")


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _is_rh() -> bool:
    chain = (os.environ.get("DESK_CHAIN") or "").strip().lower()
    if chain in ("robinhood", "rh", "rhchain"):
        return True
    state = _load_json(_STATE)
    if str(state.get("desk_chain") or "").lower() in ("robinhood", "rh", "rhchain"):
        return True
    return _RH_LOG.is_file() and (
        not _ARC_LOG.is_file()
        or _RH_LOG.stat().st_mtime >= _ARC_LOG.stat().st_mtime
    )


def _log_path() -> Path:
    return _RH_LOG if _is_rh() else _ARC_LOG


def _pnl_path() -> Path:
    return _RH_PNL if _is_rh() else _ARC_PNL


def _unit() -> str:
    return "USDG" if _is_rh() else "USDC"


def _rule_blurb() -> str:
    if _is_rh():
        return (
            "记账时的规则：Robinhood Chain Uniswap v3 短期动量（5–15 分钟），"
            "固定流动性名单，单笔硬顶 USDG，硬止损 / 止盈 / 最长持仓。"
        )
    return (
        "记账时的规则：3 到 15 分钟的 Uniswap v3 第一波，单笔 0.30 USDC，不接已经涨完的盘。"
    )


def _rows(day: str) -> list[re.Match[str]]:
    path = _log_path()
    if not path.is_file():
        return []
    out = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.startswith(day):
                continue
            match = _LINE.search(line)
            if match:
                out.append(match)
    return out


def _day_net_key() -> str:
    return "day_net_usdg" if _is_rh() else "day_net_usdc"


def _realized_key() -> str:
    return "realized_net_usdg" if _is_rh() else "realized_net_usdc"


def write_journal(day: str | None = None) -> Path:
    """Write grok-trading-desk/logs/journal/YYYY-MM-DD.md for one closed local day."""
    if _is_rh():
        from desk_realtime.rh_ledger import write_day

        return write_day(day or yesterday())
    day = day or yesterday()
    unit = _unit()
    buys = []
    closes = []
    ghosts = []
    sell_fails = 0
    halt = []
    quiet: Counter[str] = Counter()
    for match in _rows(day):
        status = match.group("status")
        sym = match.group("sym")
        note = match.group("note").strip()
        clock = match.group("clock")
        if status == "BUY" and note.startswith("BUY "):
            buys.append((clock, sym, note))
        elif status == "EXIT" and note.startswith("ghost dropped"):
            ghosts.append((clock, sym, note.split("·")[0].strip()))
        elif status == "EXIT" and note.startswith("ERROR"):
            sell_fails += 1
        elif status == "EXIT" and (
            note.startswith("HARD STOP")
            or note.startswith("TAKE PROFIT")
            or note.startswith("MAX HOLD")
            or note.startswith("TRAIL EXIT")
            or note.startswith("PANIC")
            or note.startswith("exit fired")
        ):
            closes.append((clock, sym, note))
        elif status == "BUY" and "exit rpc failed" in note:
            sell_fails += 1
        elif status == "SCAN" and sym == "BOARD" and "early quiet" in note:
            reason = note.split("·", 1)[-1].strip()
            quiet[reason] += 1
        elif "DAY HALT" in note:
            halt.append(f"{clock} {note}")

    pnl = _load_json(_pnl_path())
    state = _load_json(_STATE)
    lines = [
        f"# 交易日志 {day}",
        "",
        "本地日 00:00–24:00。只记这一天日志里真实发生的买卖，不把后来的规则写回去。",
        _rule_blurb(),
        "",
        "## 结果",
        f"- 买入 {len(buys)}",
        f"- 平仓 {len(closes)}",
        f"- 空仓丢弃 {len(ghosts)}",
        f"- 卖出失败 {sell_fails} 次",
    ]
    day_key = _day_net_key()
    real_key = _realized_key()
    if pnl.get("day") == day:
        lines.append(f"- 当日实现 {float(pnl.get(day_key) or 0):+.3f} {unit}")
    else:
        logged = 0.0
        for _, _, note in closes:
            found = _NET.search(note)
            if found:
                logged += float(found.group(1))
        lines.append(f"- 日志里平仓净额 {logged:+.3f} {unit}")
    lines.append(f"- 累计实现 {float(pnl.get(real_key) or 0):+.3f} {unit}")
    valve = str(state.get("valve_gate") or "unread")
    slots = int(state.get("slots_open") or 0)
    lines.append(f"- 写入时阀门 {valve} · 持仓 {slots}")
    if halt:
        lines.append(f"- 日亏停买 {len(halt)} 次")
    lines.extend(["", "## 平仓"])
    if closes:
        for clock, sym, note in closes:
            mult = _MULT.search(note)
            net = _NET.search(note)
            bits = [clock, f"${sym}"]
            if mult:
                bits.append(mult.group(0))
            if net:
                bits.append(f"{float(net.group(1)):+.3f} {unit}")
            head = note.split("·", 1)[0].strip()
            bits.append(head)
            lines.append("- " + " · ".join(bits))
    else:
        lines.append("- 无")
    lines.extend(["", "## 买入"])
    if buys:
        for clock, sym, note in buys:
            lines.append(f"- {clock} · ${sym} · {note}")
    else:
        lines.append("- 无")
    if ghosts:
        lines.extend(["", "## 空仓"])
        for clock, sym, note in ghosts:
            lines.append(f"- {clock} · ${sym} · {note}")
    lines.extend(["", "## 没买的原因"])
    if quiet:
        for reason, n in quiet.most_common(3):
            lines.append(f"- {n} 次 · {reason}")
    else:
        lines.append("- 这一天没有扫描安静记录")
    lines.append("")
    _DIR.mkdir(parents=True, exist_ok=True)
    path = _DIR / f"{day}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> None:
    import sys

    day = sys.argv[1] if len(sys.argv) > 1 else None
    print(write_journal(day))


if __name__ == "__main__":
    main()
