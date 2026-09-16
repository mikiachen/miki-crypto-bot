"""X (Twitter) API v2 client — recent search for narrative heat.

Env (app-only Bearer is enough for search/recent):
  X_BEARER_TOKEN=AAAA...
  # aliases: TWITTER_BEARER_TOKEN

Optional:
  X_API_ENABLED=1          # default on when bearer present
  X_SEARCH_MAX=10
  X_SEARCH_TIMEOUT=8
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

from desk_realtime.async_http import http_json
from desk_realtime.secrets import sanitize_exc

log = logging.getLogger("x_client")

_CACHE: dict[str, Any] = {}
_CACHE_TTL = float(os.environ.get("X_CACHE_TTL", "45"))


def bearer_token() -> str:
    return (
        os.environ.get("X_BEARER_TOKEN")
        or os.environ.get("TWITTER_BEARER_TOKEN")
        or os.environ.get("X_API_BEARER")
        or ""
    ).strip()


def x_api_enabled() -> bool:
    flag = os.environ.get("X_API_ENABLED", "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on"):
        return bool(bearer_token())
    # Auto: on when bearer is present
    return bool(bearer_token())


def _clean_query_token(token: str) -> str:
    t = re.sub(r"[^A-Za-z0-9_]", "", (token or "").lstrip("$"))[:32]
    return t or "Arc"


async def search_recent(
    query: str,
    *,
    max_results: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """
    GET /2/tweets/search/recent
    Returns {ok, count, tweets[{id,text,likes,rts}], query, source, error}
    """
    token = bearer_token()
    if not token:
        return {"ok": False, "count": 0, "tweets": [], "error": "X_BEARER_TOKEN missing", "source": "x"}

    max_results = max(10, min(100, int(max_results or os.environ.get("X_SEARCH_MAX", "10"))))
    timeout = float(timeout if timeout is not None else os.environ.get("X_SEARCH_TIMEOUT", "8"))
    q = (query or "").strip()[:512]
    cache_key = f"{q}|{max_results}"
    now = time.time()
    hit = _CACHE.get(cache_key)
    if hit and now - float(hit.get("ts") or 0) < _CACHE_TTL:
        out = dict(hit["data"])
        out["cached"] = True
        return out

    url = "https://api.twitter.com/2/tweets/search/recent"
    params = (
        f"?query={_urlquote(q)}"
        f"&max_results={max_results}"
        f"&tweet.fields=created_at,public_metrics,lang"
    )
    try:
        data = await http_json(
            "GET",
            url + params,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "miki-desk-x/1.0",
            },
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "count": 0,
            "tweets": [],
            "query": q,
            "error": sanitize_exc(exc),
            "source": "x",
        }

    rows = data.get("data") if isinstance(data, dict) else None
    tweets: list[dict[str, Any]] = []
    if isinstance(rows, list):
        for row in rows:
            metrics = row.get("public_metrics") or {}
            tweets.append(
                {
                    "id": str(row.get("id") or ""),
                    "text": str(row.get("text") or "")[:280],
                    "likes": int(metrics.get("like_count") or 0),
                    "rts": int(metrics.get("retweet_count") or 0),
                    "replies": int(metrics.get("reply_count") or 0),
                    "created_at": str(row.get("created_at") or ""),
                }
            )

    meta = data.get("meta") if isinstance(data, dict) else {}
    result = {
        "ok": True,
        "count": int((meta or {}).get("result_count") or len(tweets)),
        "tweets": tweets,
        "query": q,
        "source": "x",
        "error": "",
        "cached": False,
    }
    _CACHE[cache_key] = {"ts": now, "data": result}
    return result


def _urlquote(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="")


async def narrative_x_pulse(token: str, address: str = "") -> dict[str, Any]:
    """
    Build a real-time X pulse for a meme ticker / CA snippet.
    Score hint in [0,1] from mention volume + engagement.
    """
    if not x_api_enabled():
        return {
            "ok": False,
            "mention_count": 0,
            "engagement": 0,
            "score_hint": None,
            "source": "x_off",
            "error": "X API disabled or no bearer",
        }

    from desk_realtime.arc_dex import market_identity

    ident = (
        await asyncio.to_thread(market_identity, address)
        if address.startswith("0x")
        else {}
    )
    live_sym = ident.get("symbol") or ""
    desk_sym = _clean_query_token(token)
    # WARPA / WARPB are desk labels, not tickers anyone posts.
    if live_sym:
        sym = live_sym
    elif len(desk_sym) == 5 and desk_sym.startswith("WARP"):
        sym = ""
    else:
        sym = desk_sym
    cas: list[str] = []
    for raw in (ident.get("token"), address):
        a = (raw or "").strip()
        if a.startswith("0x") and len(a) == 42 and a.lower() not in cas:
            cas.append(a.lower())
    parts: list[str] = []
    if sym:
        parts.extend([f"${sym}", sym])
    parts.extend(cas)
    if not parts:
        parts = [desk_sym or "Arc"]
    # Recent-search operators. Full CA, not a 10-character prefix.
    query = "(" + " OR ".join(parts) + ") lang:en -is:retweet"

    raw = await search_recent(query)
    if not raw.get("ok") and sym:
        raw = await search_recent(f"(${sym} OR {sym}) lang:en -is:retweet")
    if not raw.get("ok"):
        return {
            "ok": False,
            "mention_count": 0,
            "engagement": 0,
            "score_hint": None,
            "source": "x",
            "error": raw.get("error") or "search failed",
            "query": raw.get("query"),
            "symbol": sym,
            "token": ident.get("token") or address,
        }

    tweets = raw.get("tweets") or []
    mentions = int(raw.get("count") or len(tweets))
    engagement = sum(
        int(t.get("likes") or 0) + 2 * int(t.get("rts") or 0) + int(t.get("replies") or 0)
        for t in tweets
    )
    # Soft map → [0,1]
    vol = 1.0 - pow(2.718281828, -mentions / 6.0)
    eng = 1.0 - pow(2.718281828, -engagement / 80.0)
    hint = max(0.0, min(1.0, 0.55 * vol + 0.45 * eng))
    samples = [t.get("text") for t in tweets[:3] if t.get("text")]
    return {
        "ok": True,
        "mention_count": mentions,
        "engagement": engagement,
        "score_hint": round(hint, 3),
        "samples": samples,
        "query": raw.get("query"),
        "symbol": sym,
        "token": ident.get("token") or address,
        "source": "x",
        "error": "",
        "cached": bool(raw.get("cached")),
    }
