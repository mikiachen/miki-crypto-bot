"""Alpha Vantage intel — underlying equity news sentiment (broadcast only).

Never a buy trigger. Used by NARRATIVE for TradFi emotion / headline context.
Requires ALPHA_VANTAGE_API_KEY (or ALPHAVANTAGE_API_KEY). Free tier is rate-limited;
results are cached aggressively.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from desk_realtime.secrets import sanitize_exc

log = logging.getLogger("alpha_vantage_intel")

_ROOT = Path(__file__).resolve().parents[1]
_CACHE_PATH = _ROOT / "grok-trading-desk" / "logs" / "alpha_vantage_intel.json"
_MEM: dict[str, Any] = {"ts": 0.0, "by_symbol": {}}


def enabled() -> bool:
    return (os.environ.get("ALPHA_VANTAGE_INTEL") or "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def api_key() -> str:
    return (
        os.environ.get("ALPHA_VANTAGE_API_KEY")
        or os.environ.get("ALPHAVANTAGE_API_KEY")
        or os.environ.get("AV_API_KEY")
        or ""
    ).strip()


def cache_sec() -> float:
    try:
        return max(300.0, float(os.environ.get("ALPHA_VANTAGE_CACHE_SEC", "1800")))
    except ValueError:
        return 1800.0


def _load_disk() -> dict[str, Any]:
    if not _CACHE_PATH.is_file():
        return {"ts": 0.0, "by_symbol": {}}
    try:
        raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return {"ts": 0.0, "by_symbol": {}}


def _save_disk(st: dict[str, Any]) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
        tmp.replace(_CACHE_PATH)
    except Exception as exc:  # noqa: BLE001
        log.debug("av cache save: %s", sanitize_exc(exc))


def _get_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "miki-rh-desk/1.0"},
    )
    with urllib.request.urlopen(req, timeout=18) as resp:
        data = json.loads(resp.read().decode())
    return data if isinstance(data, dict) else {}


def _score_feed(feed: list[dict[str, Any]], symbol: str) -> dict[str, Any]:
    sym = symbol.upper()
    scores: list[float] = []
    labels: list[str] = []
    titles: list[str] = []
    for item in feed[:12]:
        if not isinstance(item, dict):
            continue
        titles.append(str(item.get("title") or "")[:90])
        tickers = item.get("ticker_sentiment") or []
        hit = None
        for t in tickers:
            if isinstance(t, dict) and str(t.get("ticker") or "").upper() == sym:
                hit = t
                break
        if hit is not None:
            try:
                scores.append(float(hit.get("ticker_sentiment_score") or 0))
            except (TypeError, ValueError):
                pass
            labels.append(str(hit.get("ticker_sentiment_label") or ""))
        else:
            try:
                scores.append(float(item.get("overall_sentiment_score") or 0))
            except (TypeError, ValueError):
                pass
            labels.append(str(item.get("overall_sentiment_label") or ""))
    avg = sum(scores) / len(scores) if scores else 0.0
    if avg >= 0.15:
        tone = "Positive"
    elif avg <= -0.15:
        tone = "Negative"
    else:
        tone = "Neutral"
    return {
        "symbol": sym,
        "score": round(avg, 4),
        "tone": tone,
        "n": len(scores),
        "headline": titles[0] if titles else "",
        "updated_at": time.time(),
        "ok": bool(scores or titles),
    }


def fetch_sentiment(symbol: str, *, force: bool = False) -> dict[str, Any]:
    """NEWS_SENTIMENT for one ticker. Cached. Never raises into trading path."""
    sym = (symbol or "").upper().strip()
    empty = {
        "symbol": sym,
        "score": 0.0,
        "tone": "unread",
        "n": 0,
        "headline": "",
        "ok": False,
        "reason": "unread",
    }
    if not sym:
        return empty
    if not enabled():
        empty["reason"] = "ALPHA_VANTAGE_INTEL off"
        return empty
    key = api_key()
    if not key:
        empty["reason"] = "ALPHA_VANTAGE_API_KEY unread"
        return empty

    global _MEM
    if not _MEM.get("by_symbol"):
        _MEM = _load_disk()
    cached = (_MEM.get("by_symbol") or {}).get(sym)
    if (
        not force
        and isinstance(cached, dict)
        and cached.get("ok")
        and time.time() - float(cached.get("updated_at") or 0) < cache_sec()
    ):
        return dict(cached)

    qs = urllib.parse.urlencode(
        {
            "function": "NEWS_SENTIMENT",
            "tickers": sym,
            "limit": "20",
            "apikey": key,
        }
    )
    url = f"https://www.alphavantage.co/query?{qs}"
    try:
        data = _get_json(url)
    except Exception as exc:  # noqa: BLE001
        empty["reason"] = sanitize_exc(exc)
        log.info("av sentiment %s: %s", sym, empty["reason"])
        return empty
    if data.get("Note") or data.get("Information"):
        empty["reason"] = str(data.get("Note") or data.get("Information") or "rate limited")[:120]
        # keep stale cache if present
        if isinstance(cached, dict) and cached.get("ok"):
            out = dict(cached)
            out["reason"] = empty["reason"]
            return out
        return empty
    if data.get("Error Message"):
        empty["reason"] = str(data.get("Error Message"))[:120]
        return empty
    feed = data.get("feed") or []
    if not isinstance(feed, list) or not feed:
        empty["reason"] = "feed empty"
        return empty
    out = _score_feed(feed, sym)
    out["reason"] = "ok"
    by = dict(_MEM.get("by_symbol") or {})
    by[sym] = out
    _MEM = {"ts": time.time(), "by_symbol": by}
    _save_disk(_MEM)
    return out


def refresh_universe(symbols: list[str]) -> dict[str, Any]:
    """Refresh a small set (rate-limit friendly: one call per symbol with cache)."""
    rows = []
    for i, sym in enumerate(symbols):
        if i:
            time.sleep(0.35)
        rows.append(fetch_sentiment(sym))
    return {"updated_at": time.time(), "rows": rows}


def theme_line(symbols: list[str] | None = None) -> str:
    """One-line desk narrative."""
    syms = symbols or ["MSTR", "COIN", "NVDA", "TSLA"]
    bits = []
    for sym in syms[:4]:
        st = fetch_sentiment(sym)
        if not st.get("ok"):
            bits.append(f"{sym} av?")
            continue
        bits.append(f"{sym} {st.get('tone')[:3]} {float(st.get('score') or 0):+.2f}")
    head = ""
    for sym in syms:
        st = fetch_sentiment(sym)
        if st.get("headline"):
            head = str(st.get("headline"))[:64]
            break
    line = "av · " + " · ".join(bits)
    if head:
        line += f" · {head}"
    return line[:220]


def read_intel() -> dict[str, Any]:
    if not _MEM.get("by_symbol"):
        return _load_disk()
    return dict(_MEM)
