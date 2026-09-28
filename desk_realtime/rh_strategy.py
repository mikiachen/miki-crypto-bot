"""Robinhood Chain gates. Liquid swing universe (high-beta stock tokens) by default.

RH_UNIVERSE=liquid → MSTR / COIN / NVDA / TSLA with US entry windows + premium gate.
RH_UNIVERSE=meme   → legacy shallow-pool momentum board.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from desk_realtime.rh_net import USDG, WETH
from desk_realtime.secrets import sanitize_exc

log = logging.getLogger("rh_strategy")

_ROOT = Path(__file__).resolve().parents[1]
_PNL = _ROOT / "grok-trading-desk" / "logs" / "rh_realized_pnl.json"
_GECKO = "https://api.geckoterminal.com/api/v2/networks/robinhood"
_CACHE: tuple[float, list[dict[str, Any]]] = (0.0, [])
_OHLCV_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_REF_CACHE: dict[str, tuple[float, dict[str, float]]] = {}
_DAILY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_TZ_ET = ZoneInfo("America/New_York")
_TZ_SH = ZoneInfo("Asia/Shanghai")

# High-beta / crypto-native stock tokens (RHJ deployments on chain 4663).
_LIQUID = (
    ("MSTR", "0xec262a75e413fafd0df80480274532c79d42da09"),
    ("COIN", "0x6330d8c3178a418788df01a47479c0ce7ccf450b"),
    ("NVDA", "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec"),
    ("TSLA", "0x322f0929c4625ed5bad873c95208d54e1c003b2d"),
)

_LIQUID_LABEL = "MSTR·COIN·NVDA·TSLA"

# Legacy meme / community tape (shallow pools).
_MEME_SEED = (
    ("WALLET", "0x0339f5459fc690ac85f1782e15782a151b4a9e1b"),
    ("JUGGERNAUT", "0xd7321801caae694090694ff55a9323139f043b88"),
    ("FLX", "0x0d4ed27a906a0774474b200cc5392019facd2a47"),
    ("WETH", WETH),
)


def universe_mode() -> str:
    """liquid (default) | meme."""
    return (os.environ.get("RH_UNIVERSE") or "liquid").strip().lower()


def active_seed() -> tuple[tuple[str, str], ...]:
    if universe_mode() in ("meme", "trench", "legacy"):
        return _MEME_SEED
    return _LIQUID


def allowlist_addrs() -> set[str]:
    return {a.lower() for _, a in active_seed()}


def _num(raw: Any) -> float:
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def session_swing_enabled() -> bool:
    if universe_mode() in ("meme", "trench", "legacy"):
        return False
    return (os.environ.get("RH_SESSION_SWING") or "1").strip().lower() in ("1", "true", "yes", "on")


def min_m5() -> float:
    try:
        default = "0.05" if session_swing_enabled() else ("0.35" if universe_mode() == "liquid" else "1.5")
        return float(os.environ.get("RH_MIN_M5_PCT", default))
    except ValueError:
        return 0.05 if session_swing_enabled() else (0.35 if universe_mode() == "liquid" else 1.5)


def max_m15() -> float:
    try:
        return float(os.environ.get("RH_MAX_M15_PCT", "8" if universe_mode() == "liquid" else "25"))
    except ValueError:
        return 8.0 if universe_mode() == "liquid" else 25.0


def hard_stop() -> float:
    try:
        return float(os.environ.get("RH_HARD_STOP", "0.97" if universe_mode() == "liquid" else "0.92"))
    except ValueError:
        return 0.97 if universe_mode() == "liquid" else 0.92


def take_profit() -> float:
    """Backup mult cap. Primary liquid exits are RSI / EMA."""
    try:
        return float(os.environ.get("RH_TAKE_PROFIT", "1.06" if session_swing_enabled() else ("1.04" if universe_mode() == "liquid" else "1.08")))
    except ValueError:
        return 1.06 if session_swing_enabled() else (1.04 if universe_mode() == "liquid" else 1.08)


def max_hold_sec() -> float:
    """Seconds until forced flat. 0 disables time exits. Liquid default 6h safety."""
    try:
        default = "21600" if universe_mode() == "liquid" else "1800"
        return float(os.environ.get("RH_MAX_HOLD_SEC", default))
    except ValueError:
        return 21600.0 if universe_mode() == "liquid" else 1800.0


def day_loss_usdg() -> float:
    try:
        return abs(float(os.environ.get("RH_DAY_LOSS_USDG", "10")))
    except ValueError:
        return 10.0


def rsi_exit_level() -> float:
    try:
        return float(os.environ.get("RH_RSI_EXIT", "70"))
    except ValueError:
        return 70.0


def ema_stop_pct() -> float:
    """Exit when price is this % below 1h EMA55 (default 1.5)."""
    try:
        return abs(float(os.environ.get("RH_EMA_STOP_PCT", "1.5")))
    except ValueError:
        return 1.5


def max_premium_pct() -> float:
    """Block longs when token premium vs underlying exceeds this %."""
    try:
        return abs(float(os.environ.get("RH_MAX_PREMIUM_PCT", "3")))
    except ValueError:
        return 3.0


def max_book_usdg() -> float:
    """Hard cap on aggregate open notional (USDG). 0 disables."""
    try:
        return max(0.0, float(os.environ.get("RH_MAX_BOOK_USDG", "3.0")))
    except ValueError:
        return 3.0


def equity_ta_filter_enabled() -> bool:
    """Hard filter from underlying daily MACD/ATR (no LLM)."""
    if not session_swing_enabled():
        return False
    return (os.environ.get("RH_EQUITY_TA") or "1").strip().lower() in ("1", "true", "yes", "on")


def atr_extend_mult() -> float:
    """Block long if daily move already exceeds this × ATR14."""
    try:
        return abs(float(os.environ.get("RH_ATR_EXTEND_MULT", "1.5")))
    except ValueError:
        return 1.5


def liquid_universe_label() -> str:
    return _LIQUID_LABEL


def us_equity_session(now: datetime | None = None) -> str:
    """US equity session buckets (ET).

    pre_break   — 07:30–09:30 盘前
    regular     — 09:30–15:00 常规盘中（RH_INTRADAY=1 时可开仓）
    power_hour  — 15:00–16:00 尾盘
    weekend     — Sat/Sun: no new entries
    off         — overnight / closed
    """
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    et = dt.astimezone(_TZ_ET)
    if et.weekday() >= 5:
        return "weekend"
    mins = et.hour * 60 + et.minute
    if 15 * 60 <= mins < 16 * 60:
        return "power_hour"
    if 9 * 60 + 30 <= mins < 15 * 60:
        return "regular"
    if 7 * 60 + 30 <= mins < 9 * 60 + 30:
        return "pre_break"
    return "off"


def intraday_enabled() -> bool:
    """Allow entries during regular RTH (09:30–15:00 ET). Default on for desk flow."""
    return (os.environ.get("RH_INTRADAY") or "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def session_label() -> str:
    return us_equity_session()


def entry_window_open(sess: str | None = None) -> bool:
    s = sess or us_equity_session()
    if s in ("pre_break", "power_hour"):
        return True
    if s == "regular" and intraday_enabled():
        return True
    return False


def breakout_mode() -> str:
    """strict | near | momentum — how hard the 1–4h breakout gate is."""
    raw = (os.environ.get("RH_BREAKOUT_MODE") or "near").strip().lower()
    if raw in ("strict", "near", "momentum"):
        return raw
    return "near"


def underlying_ref(symbol: str) -> dict[str, float]:
    """Yahoo underlying: live + previous close. Cached ~10 min.

    Premium uses previous close on weekend/off (最新收盘价); live in entry windows.
    """
    sym = (symbol or "").upper().strip()
    empty = {"live": 0.0, "prev_close": 0.0, "ref": 0.0}
    if not sym or sym in ("WETH", "USDG"):
        return empty
    hit = _REF_CACHE.get(sym)
    if hit and time.time() - hit[0] < 600:
        out = dict(hit[1])
    else:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 miki-rh-desk/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            meta = (((data.get("chart") or {}).get("result") or [{}])[0].get("meta") or {})
            live = _num(meta.get("regularMarketPrice"))
            prev = _num(meta.get("chartPreviousClose") or meta.get("previousClose"))
            out = {"live": live, "prev_close": prev, "ref": 0.0}
            if live > 0 or prev > 0:
                _REF_CACHE[sym] = (time.time(), dict(out))
        except Exception as exc:  # noqa: BLE001
            log.info("underlying ref %s: %s", sym, sanitize_exc(exc))
            return empty
    live = float(out.get("live") or 0)
    prev = float(out.get("prev_close") or 0)
    sess = us_equity_session()
    if entry_window_open(sess) and live > 0:
        ref = live
    elif prev > 0:
        ref = prev
    else:
        ref = live
    out["ref"] = ref
    return out


def token_premium_pct(token_px: float, symbol: str) -> tuple[float, str]:
    """(premium%, reason). Prefer RHJ mid, else Yahoo ref."""
    base = 0.0
    src = ""
    try:
        from desk_realtime.rhj_market import enabled as rhj_on, mid_price

        if rhj_on():
            mid = mid_price(symbol)
            if mid > 0:
                base = mid
                src = f"rhj {mid:.2f}"
    except Exception:
        pass
    if base <= 0:
        ref = underlying_ref(symbol)
        base = float(ref.get("ref") or 0)
        src = f"yahoo {base:.2f}" if base > 0 else "ref unread"
    if base <= 0 or token_px <= 0:
        return 0.0, "premium ref unread"
    prem = (float(token_px) - base) / base * 100.0
    return prem, src


def portfolio_block(*, open_notional: float, next_size: float) -> tuple[str, str]:
    """(audit_code, reason). Empty code = pass."""
    cap = max_book_usdg()
    if cap <= 0:
        return "", ""
    if float(open_notional) + float(next_size) > cap + 1e-9:
        return (
            "BOOK_CAP",
            f"book {float(open_notional):.2f}+{float(next_size):.2f} > max {cap:.2f} USDG",
        )
    return "", ""


def format_audit(code: str, reason: str) -> str:
    code = (code or "").strip()
    reason = (reason or "").strip()
    if code and reason:
        return f"[{code}] {reason}"
    return reason or code


def _ema_series(closes: list[float], period: int) -> list[float]:
    if len(closes) < period or period <= 0:
        return []
    k = 2.0 / (period + 1.0)
    out: list[float] = [0.0] * len(closes)
    seed = sum(closes[:period]) / float(period)
    out[period - 1] = seed
    ema = seed
    for i in range(period, len(closes)):
        ema = closes[i] * k + ema * (1.0 - k)
        out[i] = ema
    return out


def _atr_series(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float]:
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return []
    trs: list[float] = [0.0]
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    out = [0.0] * n
    atr = sum(trs[1 : period + 1]) / float(period)
    out[period] = atr
    for i in range(period + 1, n):
        atr = (atr * (period - 1) + trs[i]) / float(period)
        out[i] = atr
    return out


def underlying_daily_ta(symbol: str) -> dict[str, Any]:
    """Yahoo daily MACD(12,26,9) + ATR14 for hard equity filter. Cached ~30 min."""
    sym = (symbol or "").upper().strip()
    empty: dict[str, Any] = {
        "ok": False,
        "symbol": sym,
        "close": 0.0,
        "macd": 0.0,
        "signal": 0.0,
        "hist": 0.0,
        "atr": 0.0,
        "day_range": 0.0,
    }
    if not sym or sym in ("WETH", "USDG"):
        return empty
    hit = _DAILY_CACHE.get(sym)
    if hit and time.time() - hit[0] < 1800:
        return dict(hit[1])
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=6mo&interval=1d"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 miki-rh-desk/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
        result = ((data.get("chart") or {}).get("result") or [None])[0] or {}
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        closes_raw = quote.get("close") or []
        highs_raw = quote.get("high") or []
        lows_raw = quote.get("low") or []
        closes: list[float] = []
        highs: list[float] = []
        lows: list[float] = []
        for c, h, low in zip(closes_raw, highs_raw, lows_raw):
            if c is None or h is None or low is None:
                continue
            try:
                cf, hf, lf = float(c), float(h), float(low)
            except (TypeError, ValueError):
                continue
            if cf > 0 and hf > 0 and lf > 0:
                closes.append(cf)
                highs.append(hf)
                lows.append(lf)
        if len(closes) < 40:
            return empty
        ema12 = _ema_series(closes, 12)
        ema26 = _ema_series(closes, 26)
        macd_line: list[float] = []
        for i in range(len(closes)):
            if i < 25 or not ema12 or not ema26:
                macd_line.append(0.0)
            else:
                macd_line.append(ema12[i] - ema26[i])
        # signal from macd values starting when both emas live
        macd_valid = macd_line[25:]
        sig_series = _ema_series(macd_valid, 9)
        if len(sig_series) < 9:
            return empty
        # align: last signal corresponds to last close
        hist_vals: list[float] = []
        signal_full = [0.0] * len(closes)
        # sig_series[i] aligns with macd_valid[i] = macd_line[25+i]
        for i, sig in enumerate(sig_series):
            idx = 25 + i
            if idx < len(closes) and sig != 0.0 or i >= 8:
                signal_full[idx] = sig
                hist_vals.append(macd_line[idx] - sig)
        atr_s = _atr_series(highs, lows, closes, 14)
        i = len(closes) - 1
        macd = macd_line[i]
        # find last non-zero signal
        signal = 0.0
        for j in range(i, 24, -1):
            if signal_full[j] != 0.0 or j >= 25 + 8:
                signal = signal_full[j]
                if signal != 0.0 or j == i:
                    break
        # recompute signal cleanly
        signal = sig_series[-1] if sig_series else 0.0
        hist = macd - signal
        atr = atr_s[i] if atr_s and i < len(atr_s) else 0.0
        day_range = highs[i] - lows[i]
        out = {
            "ok": True,
            "symbol": sym,
            "close": closes[i],
            "macd": round(macd, 4),
            "signal": round(signal, 4),
            "hist": round(hist, 4),
            "atr": round(atr, 4),
            "day_range": round(day_range, 4),
        }
        _DAILY_CACHE[sym] = (time.time(), dict(out))
        return out
    except Exception as exc:  # noqa: BLE001
        log.info("underlying daily ta %s: %s", sym, sanitize_exc(exc))
        return empty


def equity_ta_block(symbol: str) -> str:
    """Empty = pass. Uses underlying daily MACD/ATR only (no news/LLM).

    Soft mode (default when RH_EQUITY_TA=soft): unread passes; only block
    strongly bearish MACD hist or ATR-extended days.
    """
    if not equity_ta_filter_enabled():
        return ""
    mode = (os.environ.get("RH_EQUITY_TA_MODE") or "soft").strip().lower()
    ta = underlying_daily_ta(symbol)
    if not ta.get("ok"):
        # Soft: do not freeze the desk when Yahoo is slow.
        if mode in ("soft", "off", "0"):
            return ""
        return "equity TA unread · no long"
    hist = float(ta.get("hist") or 0)
    if mode in ("soft",):
        # Only block clearly bearish histogram (was: any hist < 0).
        if hist < -0.5:
            return f"equity MACD hist {hist:+.3f} strongly bearish"
    elif hist < 0:
        return f"equity MACD hist {hist:+.3f} bearish"
    atr = float(ta.get("atr") or 0)
    day_range = float(ta.get("day_range") or 0)
    mult = atr_extend_mult()
    if atr > 0 and day_range >= atr * mult:
        return f"equity day range {day_range:.2f} ≥ {mult:.1f}×ATR {atr:.2f}"
    return ""


def _ema(closes: list[float], period: int) -> float:
    if len(closes) < period or period <= 0:
        return 0.0
    k = 2.0 / (period + 1.0)
    ema = sum(closes[:period]) / float(period)
    for px in closes[period:]:
        ema = px * k + ema * (1.0 - k)
    return float(ema)


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss <= 1e-12:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


def _get_json(url: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "miki-rh-desk/1.0"},
    )
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            msg = str(exc)
            if "429" in msg or "Too Many" in msg:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise
    raise last_exc or RuntimeError("gecko unread")


def _ohlcv_side(pool_name: str, symbol: str) -> str:
    """Gecko OHLCV token=base|quote — USDG/WETH pools need quote for WETH USD."""
    name = (pool_name or "").upper().replace(" ", "")
    sym = (symbol or "").upper()
    if not sym:
        return "base"
    if name.startswith(sym + "/") or name.startswith(sym + "0."):
        return "base"
    if f"/{sym}" in name or name.endswith("/" + sym):
        return "quote"
    return "base"


def _ohlcv_closes(
    pool: str,
    *,
    aggregate: int,
    limit: int = 80,
    side: str = "base",
) -> list[tuple[int, float, float]]:
    """Return [(ts, close, high), ...] oldest→newest from Gecko pool OHLCV."""
    pool = (pool or "").strip()
    if not (pool.startswith("0x") and len(pool) == 42):
        return []
    side = "quote" if side == "quote" else "base"
    key = f"{pool.lower()}:h{aggregate}:{side}"
    hit = _OHLCV_CACHE.get(key)
    if hit and time.time() - hit[0] < 120:
        return list(hit[1].get("bars") or [])
    try:
        data = _get_json(
            f"{_GECKO}/pools/{pool}/ohlcv/hour?aggregate={int(aggregate)}"
            f"&limit={int(limit)}&currency=usd&token={side}"
        )
    except Exception as exc:  # noqa: BLE001
        log.info("rh ohlcv %s h%s %s: %s", pool[:10], aggregate, side, sanitize_exc(exc))
        return list((hit[1].get("bars") if hit else []) or [])
    raw = ((data.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    bars: list[tuple[int, float, float]] = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        try:
            ts = int(row[0])
            high = float(row[2])
            close = float(row[4])
        except (TypeError, ValueError):
            continue
        if close > 0:
            bars.append((ts, close, high))
    bars.sort(key=lambda x: x[0])
    if bars:
        _OHLCV_CACHE[key] = (time.time(), {"bars": bars})
    return bars


def swing_snapshot(pool: str, *, token: str = "", symbol: str = "", pool_name: str = "") -> dict[str, Any]:
    """1h/4h EMA21/55 + RSI14 from pool USD closes."""
    empty = {
        "ok": False,
        "token": token,
        "pool": pool,
        "px": 0.0,
        "ema21_4h": 0.0,
        "ema55_4h": 0.0,
        "ema21_1h": 0.0,
        "ema55_1h": 0.0,
        "rsi_1h": 50.0,
        "range_high_12h": 0.0,
        "session": us_equity_session(),
    }
    side = _ohlcv_side(pool_name, symbol)
    h1 = _ohlcv_closes(pool, aggregate=1, limit=80, side=side)
    h4 = _ohlcv_closes(pool, aggregate=4, limit=80, side=side)
    if len(h1) < 56 or len(h4) < 56:
        return empty
    c1 = [c for _, c, _ in h1]
    c4 = [c for _, c, _ in h4]
    highs_12 = [h for _, _, h in h1[-12:]]
    return {
        "ok": True,
        "token": token,
        "pool": pool,
        "px": float(c1[-1]),
        "ema21_4h": _ema(c4, 21),
        "ema55_4h": _ema(c4, 55),
        "ema21_1h": _ema(c1, 21),
        "ema55_1h": _ema(c1, 55),
        "rsi_1h": _rsi(c1, 14),
        "range_high_12h": max(highs_12) if highs_12 else 0.0,
        "session": us_equity_session(),
        "ohlcv_side": side,
    }


def swing_for_token(token: str) -> dict[str, Any]:
    tok = (token or "").lower()
    for row in liquid_board():
        if str(row.get("address") or "").lower() == tok:
            sw = row.get("swing")
            if isinstance(sw, dict) and sw.get("ok"):
                return sw
            pool = str(row.get("pool") or "")
            return swing_snapshot(
                pool,
                token=tok,
                symbol=str(row.get("symbol") or ""),
                pool_name=str(row.get("pool_name") or ""),
            )
    return swing_snapshot("", token=tok)


def _liquid_entry_block(row: dict[str, Any]) -> str:
    """Entry in pre_break / regular / power_hour with premium + RHJ + soft tape gates."""
    sess = us_equity_session()
    if not entry_window_open(sess):
        row["audit_code"] = "SESSION"
        return format_audit("SESSION", f"{sess} · no entry window")

    sw = row.get("swing") if isinstance(row.get("swing"), dict) else {}
    if not sw.get("ok"):
        row["audit_code"] = "SWING"
        return format_audit("SWING", "swing tape unread")
    px = float(sw.get("px") or 0)
    e55_4 = float(sw.get("ema55_4h") or 0)
    rsi_1 = float(sw.get("rsi_1h") or 50)
    if px <= 0 or e55_4 <= 0:
        row["audit_code"] = "SWING"
        return format_audit("SWING", "swing levels unread")

    sym = str(row.get("symbol") or "")
    addr = str(row.get("address") or "")

    # RHJ native halt / capabilities / address check.
    try:
        from desk_realtime.rhj_market import gate as rhj_gate

        code, why = rhj_gate(sym, session=sess, token_address=addr)
        if code:
            row["audit_code"] = code
            return format_audit(code, why)
    except Exception as exc:  # noqa: BLE001
        row["audit_code"] = "RHJ_ERR"
        return format_audit("RHJ_ERR", sanitize_exc(exc)[:80])

    prem, prem_why = token_premium_pct(px, sym)
    row["premium_pct"] = round(prem, 3)
    row["premium_ref"] = prem_why
    if "unread" in prem_why:
        row["audit_code"] = "PREMIUM"
        return format_audit("PREMIUM", "premium ref unread · no long")
    if prem > max_premium_pct():
        row["audit_code"] = "PREMIUM"
        return format_audit(
            "PREMIUM",
            f"premium {prem:+.1f}% > {max_premium_pct():.0f}% · {prem_why} · no long",
        )

    # Underlying daily MACD/ATR (soft by default — see equity_ta_block).
    ta = underlying_daily_ta(sym)
    row["equity_ta"] = ta
    ta_block = equity_ta_block(sym)
    if ta_block:
        row["audit_code"] = "EQUITY_TA"
        return format_audit("EQUITY_TA", ta_block)

    m15 = float(row.get("chg_m15") or 0)
    if m15 >= max_m15():
        row["audit_code"] = "EXTEND"
        return format_audit("EXTEND", f"already extended +{m15:.1f}% 15m")

    # Tape gates: breakout can be strict / near / momentum.
    rh = float(sw.get("range_high_12h") or 0)
    m5 = float(row.get("chg_m5") or 0)
    h1 = float(row.get("chg_h1") or 0)
    mode = breakout_mode()
    if rh <= 0 and mode == "strict":
        row["audit_code"] = "BREAKOUT"
        return format_audit("BREAKOUT", "breakout range unread")
    if mode == "strict":
        if px < rh * 1.0005:
            row["audit_code"] = "BREAKOUT"
            return format_audit("BREAKOUT", "no 1-4h breakout")
    elif mode == "near":
        # Within 0.4% of range high counts as actionable (desk flow).
        if rh > 0 and px < rh * 0.996:
            if not (m5 >= min_m5() or h1 >= 0.20):
                row["audit_code"] = "BREAKOUT"
                return format_audit("BREAKOUT", "no near-breakout / momentum")
    else:  # momentum
        if m5 < min_m5() and h1 < 0.15:
            row["audit_code"] = "MOMENTUM"
            return format_audit("MOMENTUM", "momentum flat")

    if px < e55_4 * 0.998:
        row["audit_code"] = "TREND"
        return format_audit("TREND", "4h still weak")
    if rsi_1 >= 75:
        row["audit_code"] = "RSI"
        return format_audit("RSI", f"RSI {rsi_1:.0f} extended")
    if mode == "strict" and m5 < min_m5() and h1 < 0.12:
        row["audit_code"] = "MOMENTUM"
        return format_audit("MOMENTUM", "breakout momentum flat")
    row["audit_code"] = "PASS"
    return ""


def should_exit(*, live_mult: float, held_sec: float, token: str = "") -> tuple[bool, str]:
    if live_mult > 0 and live_mult <= hard_stop():
        return True, "HARD STOP"

    if session_swing_enabled() and token:
        sw = swing_for_token(token)
        if sw.get("ok"):
            rsi_1 = float(sw.get("rsi_1h") or 50)
            px = float(sw.get("px") or 0)
            e55_1 = float(sw.get("ema55_1h") or 0)
            if rsi_1 >= rsi_exit_level():
                return True, "RSI TAKE"
            if px > 0 and e55_1 > 0 and px < e55_1 * (1.0 - ema_stop_pct() / 100.0):
                return True, "EMA STOP"

    if live_mult >= take_profit():
        return True, "TAKE PROFIT"
    hold = max_hold_sec()
    if hold > 0 and held_sec >= hold:
        return True, "MAX HOLD"
    return False, ""


def _row_from_pool(attr: dict[str, Any], *, symbol: str, address: str, ver: str = "v3") -> dict[str, Any]:
    chg = attr.get("price_change_percentage") if isinstance(attr.get("price_change_percentage"), dict) else {}
    vol = attr.get("volume_usd") if isinstance(attr.get("volume_usd"), dict) else {}
    return {
        "symbol": symbol[:16],
        "address": address.lower(),
        "pool": str(attr.get("address") or ""),
        "pool_name": str(attr.get("name") or ""),
        "dex": "uniswap",
        "labels": [ver],
        "liquidity_usdg": round(_num(attr.get("reserve_in_usd")), 2),
        "chg_m5": round(_num(chg.get("m5")), 3),
        "chg_m15": round(_num(chg.get("m15")), 3),
        "chg_h1": round(_num(chg.get("h1")), 3),
        "volume_m5": round(_num(vol.get("m5")), 2),
        "volume_m15": round(_num(vol.get("m15")), 2),
        "quote": USDG.lower(),
        "seed": True,
    }


def _best_usdg_pool(token: str, symbol: str) -> dict[str, Any] | None:
    """Pick the deepest Uniswap pool vs USDG (or WETH) for an allowlisted token."""
    try:
        data = _get_json(f"{_GECKO}/tokens/{token}/pools?page=1")
    except Exception as exc:  # noqa: BLE001
        log.info("rh token pools %s: %s", symbol, sanitize_exc(exc))
        return None
    best: dict[str, Any] | None = None
    best_liq = -1.0
    for row in data.get("data") or []:
        if not isinstance(row, dict):
            continue
        attr = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
        name = str(attr.get("name") or "").upper()
        rel = row.get("relationships") if isinstance(row.get("relationships"), dict) else {}
        dex_id = str(((rel.get("dex") or {}).get("data") or {}).get("id") or "").lower()
        if "uniswap" not in dex_id:
            continue
        if "v4" in dex_id:
            ver = "v4"
        elif "v3" in dex_id:
            ver = "v3"
        else:
            ver = "v4" if ("USDG" in name or "WETH" in name) else ""
        if not ver:
            continue
        if "USDG" not in name and "WETH" not in name and "USD" not in name:
            continue
        liq = _num(attr.get("reserve_in_usd"))
        score = liq + (1e12 if "USDG" in name else 0.0)
        # Prefer TOKEN/USDG orientation so OHLCV base side matches the asset.
        if name.startswith(symbol.upper() + " ") or name.startswith(symbol.upper() + "/"):
            score += 1e11
        # Prefer v3 — more reliable hour OHLCV than some v4 pool ids.
        if ver == "v3":
            score += 5e10
        if score > best_liq:
            best_liq = score
            best = _row_from_pool(attr, symbol=symbol, address=token, ver=ver)
    return best


def _attach_swing(row: dict[str, Any]) -> dict[str, Any]:
    if not session_swing_enabled():
        return row
    pool = str(row.get("pool") or "")
    tok = str(row.get("address") or "")
    row["swing"] = swing_snapshot(
        pool,
        token=tok,
        symbol=str(row.get("symbol") or ""),
        pool_name=str(row.get("pool_name") or ""),
    )
    row["session"] = us_equity_session()
    return row


def _liquid_board(limit: int) -> list[dict[str, Any]]:
    """Allowlist-only board: WETH + stock tokens from their deepest pools."""
    out: list[dict[str, Any]] = []
    for i, (sym, addr) in enumerate(active_seed()):
        if i:
            time.sleep(0.8)  # soft rate-limit vs Gecko
        row = _best_usdg_pool(addr, sym)
        if row is None:
            row = {
                "symbol": sym,
                "address": addr.lower(),
                "pool": "",
                "pool_name": "",
                "dex": "uniswap",
                "labels": ["v3", "v4"],
                "liquidity_usdg": 0.0,
                "chg_m5": 0.0,
                "chg_m15": 0.0,
                "chg_h1": 0.0,
                "volume_m5": 0.0,
                "volume_m15": 0.0,
                "quote": USDG.lower(),
                "seed": True,
            }
        out.append(_attach_swing(row))
        time.sleep(0.6)
    out.sort(key=lambda r: float(r.get("liquidity_usdg") or 0), reverse=True)
    return out[:limit]


def liquid_board(limit: int = 20) -> list[dict[str, Any]]:
    """Board tape. Cached ~90s in session-swing (OHLCV heavy), else ~45s."""
    global _CACHE
    ts, rows = _CACHE
    mode = universe_mode()
    ttl = 90.0 if session_swing_enabled() else 45.0
    if rows and time.time() - ts < ttl:
        return rows[:limit]
    if mode not in ("meme", "trench", "legacy"):
        out = _liquid_board(limit)
        _CACHE = (time.time(), out)
        return out[:limit]

    out: list[dict[str, Any]] = []
    try:
        data = _get_json(f"{_GECKO}/trending_pools?page=1&include=base_token,quote_token,dex")
    except Exception as exc:  # noqa: BLE001
        log.info("rh board: %s", sanitize_exc(exc))
        return list(_CACHE[1])[:limit] if _CACHE[1] else []
    included = {
        str(item.get("id") or ""): (item.get("attributes") or {})
        for item in (data.get("included") or [])
        if isinstance(item, dict) and item.get("type") == "token"
    }
    allow = allowlist_addrs()
    for row in data.get("data") or []:
        if not isinstance(row, dict):
            continue
        attr = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
        rel = row.get("relationships") if isinstance(row.get("relationships"), dict) else {}
        dex_id = str(((rel.get("dex") or {}).get("data") or {}).get("id") or "")
        if "uniswap" not in dex_id:
            continue
        low_dex = dex_id.lower()
        if "v4" in low_dex:
            ver = "v4"
        elif "v3" in low_dex:
            ver = "v3"
        else:
            continue
        base_id = str(((rel.get("base_token") or {}).get("data") or {}).get("id") or "")
        quote_id = str(((rel.get("quote_token") or {}).get("data") or {}).get("id") or "")
        base = included.get(base_id) or {}
        quote = included.get(quote_id) or {}
        base_addr = str(base.get("address") or "").lower()
        quote_addr = str(quote.get("address") or "").lower()
        if not (base_addr.startswith("0x") and len(base_addr) == 42):
            continue
        quote_is_stable = quote_addr in (USDG.lower(), WETH.lower())
        if not quote_is_stable and base_addr not in (USDG.lower(), WETH.lower()):
            continue
        token = base_addr
        if base_addr in (USDG.lower(),):
            continue
        if allow and token not in allow:
            continue
        symbol = str(base.get("symbol") or attr.get("name") or "?").split("/")[0].strip().upper()
        chg = attr.get("price_change_percentage") if isinstance(attr.get("price_change_percentage"), dict) else {}
        vol = attr.get("volume_usd") if isinstance(attr.get("volume_usd"), dict) else {}
        out.append({
            "symbol": symbol[:16],
            "address": token,
            "pool": str(attr.get("address") or ""),
            "dex": "uniswap",
            "labels": [ver],
            "liquidity_usdg": round(_num(attr.get("reserve_in_usd")), 2),
            "chg_m5": round(_num(chg.get("m5")), 3),
            "chg_m15": round(_num(chg.get("m15")), 3),
            "chg_h1": round(_num(chg.get("h1")), 3),
            "volume_m5": round(_num(vol.get("m5")), 2),
            "volume_m15": round(_num(vol.get("m15")), 2),
            "quote": quote_addr,
        })
        if len(out) >= limit:
            break
    if not out:
        for sym, addr in active_seed():
            out.append({
                "symbol": sym,
                "address": addr.lower(),
                "pool": "",
                "dex": "uniswap",
                "labels": ["v3"],
                "liquidity_usdg": 0.0,
                "chg_m5": 0.0,
                "chg_m15": 0.0,
                "chg_h1": 0.0,
                "volume_m5": 0.0,
                "volume_m15": 0.0,
                "quote": USDG.lower(),
                "seed": True,
            })
    _CACHE = (time.time(), out)
    return out[:limit]


def trade_block(row: dict[str, Any] | None) -> str:
    """Empty string means the tape may quote."""
    if not row:
        return format_audit("BOARD", "not on rh board")
    if str(row.get("dex") or "") != "uniswap":
        row["audit_code"] = "ROUTE"
        return format_audit("ROUTE", "route unread")
    addr = str(row.get("address") or "").lower()
    if addr and addr not in allowlist_addrs() and universe_mode() not in ("meme", "trench", "legacy"):
        row["audit_code"] = "UNIVERSE"
        return format_audit("UNIVERSE", "not in liquid universe")
    labels = [str(x).lower() for x in (row.get("labels") or [])]
    if "v3" not in labels and "v4" not in labels:
        row["audit_code"] = "POOL"
        return format_audit("POOL", "v3/v4 only")
    liq = float(row.get("liquidity_usdg") or 0)
    min_liq = float(os.environ.get("RH_MIN_LIQ_USDG", "50000"))
    if not row.get("seed") and liq < min_liq:
        row["audit_code"] = "LIQ"
        return format_audit("LIQ", "pool too thin")
    if row.get("seed") and liq > 0 and liq < min_liq * 0.25:
        row["audit_code"] = "LIQ"
        return format_audit("LIQ", "pool too thin")

    if session_swing_enabled():
        return _liquid_entry_block(row)

    m5 = float(row.get("chg_m5") or 0)
    m15 = float(row.get("chg_m15") or 0)
    if m5 < min_m5():
        row["audit_code"] = "MOMENTUM"
        return format_audit("MOMENTUM", f"m5 flat {m5:+.2f}%")
    if m15 >= max_m15():
        row["audit_code"] = "EXTEND"
        return format_audit("EXTEND", f"already extended +{m15:.1f}% 15m")
    if universe_mode() in ("meme", "trench", "legacy"):
        vol_m5 = float(row.get("volume_m5") or 0)
        vol_m15 = float(row.get("volume_m15") or 0)
        if vol_m15 <= 0 or (vol_m5 / 5.0) <= (vol_m15 / 15.0) * 0.9:
            row["audit_code"] = "VOLUME"
            return format_audit("VOLUME", "volume not expanding")
    row["audit_code"] = "PASS"
    return ""


def pick_short_target(exclude: set[str] | None = None) -> dict[str, Any] | None:
    held = {a.lower() for a in (exclude or set()) if a}
    for row in liquid_board():
        addr = str(row.get("address") or "").lower()
        if not addr or addr in held:
            continue
        if trade_block(row):
            continue
        return dict(row)
    return None


def quiet_reason() -> str:
    from collections import Counter

    sess = us_equity_session()
    counts: Counter[str] = Counter()
    for row in liquid_board():
        why = trade_block(row)
        code = str(row.get("audit_code") or "").strip()
        if not why:
            key = "PASS"
        elif code:
            key = code
        else:
            key = why.split("·")[0].strip()[:36]
        counts[key] += 1
    top = counts.most_common(2)
    if not top:
        return f"{sess} · board unread"
    detail = " · ".join(f"{n}× {reason}" for reason, n in top)
    return f"{sess} · {detail}"


def _pnl_load() -> dict[str, Any]:
    if not _PNL.is_file():
        return {"realized_net_usdg": 0.0, "day_net_usdg": 0.0, "closes": 0}
    try:
        return json.loads(_PNL.read_text(encoding="utf-8"))
    except Exception:
        return {"realized_net_usdg": 0.0, "closes": 0}


def _pnl_save(st: dict[str, Any]) -> None:
    _PNL.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PNL.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
    tmp.replace(_PNL)


def record_close_pnl(*, entry_usdg: float, exit_mult: float, gas_usdg: float = 0.02) -> dict[str, Any]:
    gross = float(entry_usdg) * (float(exit_mult) - 1.0)
    net = gross - float(gas_usdg)
    st = _pnl_load()
    day = time.strftime("%Y-%m-%d", time.localtime())
    if st.get("day") != day:
        st["day"] = day
        st["day_net_usdg"] = 0.0
    st["realized_net_usdg"] = float(st.get("realized_net_usdg") or 0) + net
    st["day_net_usdg"] = float(st.get("day_net_usdg") or 0) + net
    st["closes"] = int(st.get("closes") or 0) + 1
    st["ts"] = time.time()
    _pnl_save(st)
    return st


def realized_snapshot() -> dict[str, Any]:
    return _pnl_load()


def day_loss_halt() -> tuple[bool, str]:
    st = _pnl_load()
    day = time.strftime("%Y-%m-%d", time.localtime())
    if st.get("day") != day:
        return False, ""
    net = float(st.get("day_net_usdg") or 0)
    cap = day_loss_usdg()
    if net <= -cap:
        return True, f"day loss {net:.2f} ≤ -{cap:.2f}"
    return False, ""
