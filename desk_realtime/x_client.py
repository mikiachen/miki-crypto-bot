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


_RESERVED_HANDLES = {
    "home", "search", "explore", "intent", "share", "i", "hashtag", "login", "privacy",
}


def handle_from_url(url: str) -> str:
    """Profile handle from a listed X URL. Tweet and intent links are ignored."""
    from urllib.parse import urlparse

    raw = (url or "").strip()
    if raw.startswith("@") and "/" not in raw:
        name = raw[1:]
    else:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = parsed.netloc.lower()
        parts = [p for p in parsed.path.split("/") if p]
        if not (host.endswith("x.com") or host.endswith("twitter.com")) or len(parts) != 1:
            return ""
        name = parts[0]
    name = re.sub(r"[^A-Za-z0-9_]", "", name)
    if not name or name.isdigit() or len(name) > 15 or name.lower() in _RESERVED_HANDLES:
        return ""
    return name


def _domain(url: str) -> str:
    from urllib.parse import urlparse

    host = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _age_days(created_at: str) -> float | None:
    from datetime import datetime, timezone

    raw = (created_at or "").strip()
    if not raw:
        return None
    try:
        created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 86400.0)


def _compact(n: int | None) -> str:
    if n is None:
        return "?"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _mismatch(text: str, symbol: str, address: str) -> bool:
    """Listed account talks about a different ticker or CA, and not this one."""
    blob = text or ""
    sym = (symbol or "").lstrip("$").upper()
    mentions_us = bool(sym) and (
        f"${sym}".lower() in blob.lower() or sym.lower() in blob.lower()
    )
    if address and address.startswith("0x"):
        found = re.findall(r"0x[a-fA-F0-9]{40}", blob)
        if found and all(a.lower() != address.lower() for a in found):
            return True
        if any(a.lower() == address.lower() for a in found):
            mentions_us = True
    others = [
        t for t in re.findall(r"\$([A-Za-z][A-Za-z0-9]{1,12})", blob)
        if t.upper() != sym
    ]
    return bool(others) and not mentions_us


async def lookup_user(username: str, *, timeout: float | None = None) -> dict[str, Any]:
    """GET /2/users/by/username. 404 is a dead listed handle, not a guessed one."""
    token = bearer_token()
    name = re.sub(r"[^A-Za-z0-9_]", "", username or "")
    if not token or not name:
        return {"ok": False, "error": "no bearer or handle"}
    timeout = float(timeout if timeout is not None else os.environ.get("X_SEARCH_TIMEOUT", "8"))
    cache_key = f"user|{name.lower()}"
    now = time.time()
    hit = _CACHE.get(cache_key)
    if hit and now - float(hit.get("ts") or 0) < 900:
        out = dict(hit["data"])
        out["cached"] = True
        return out
    url = (
        f"https://api.twitter.com/2/users/by/username/{name}"
        "?user.fields=created_at,description,public_metrics,verified,verified_type,url,entities"
    )
    try:
        data = await http_json(
            "GET",
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "miki-desk-x/1.0",
            },
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        err = sanitize_exc(exc)
        dead = "404" in err
        return {"ok": False, "error": err, "dead": dead, "username": name}
    user = data.get("data") if isinstance(data, dict) else None
    if not isinstance(user, dict):
        return {"ok": False, "error": "user unread", "username": name}
    metrics = user.get("public_metrics") if isinstance(user.get("public_metrics"), dict) else {}
    entities = user.get("entities") if isinstance(user.get("entities"), dict) else {}
    url_ent = entities.get("url") if isinstance(entities.get("url"), dict) else {}
    urls = url_ent.get("urls") if isinstance(url_ent.get("urls"), list) else []
    expanded = ""
    if urls and isinstance(urls[0], dict):
        expanded = str(urls[0].get("expanded_url") or urls[0].get("url") or "")
    result = {
        "ok": True,
        "username": str(user.get("username") or name),
        "name": str(user.get("name") or ""),
        "description": str(user.get("description") or "")[:280],
        "created_at": str(user.get("created_at") or ""),
        "followers": int(metrics.get("followers_count") or 0),
        "tweet_count": int(metrics.get("tweet_count") or 0),
        "verified": bool(user.get("verified")),
        "verified_type": str(user.get("verified_type") or ""),
        "url": expanded or str(user.get("url") or ""),
        "error": "",
        "dead": False,
        "cached": False,
    }
    _CACHE[cache_key] = {"ts": now, "data": result}
    return result


async def official_account(token: str, address: str = "") -> dict[str, Any]:
    """
    Credibility of the handle Dexscreener lists for this token.
    Never searches X for a username invented from the ticker.
    """
    empty = {
        "ok": False,
        "cred": "unread",
        "handle": "",
        "age_days": None,
        "followers": None,
        "verified_type": "",
        "site_match": None,
        "line": "official unread",
        "error": "",
    }
    if not x_api_enabled():
        empty["cred"] = "unread"
        empty["line"] = "official unread · X off"
        empty["error"] = "X API disabled or no bearer"
        return empty
    try:
        from desk_realtime.arc_dex import pair_links

        links = await asyncio.to_thread(pair_links, address) if address.startswith("0x") else {}
    except Exception as exc:  # noqa: BLE001
        empty["error"] = sanitize_exc(exc)
        empty["line"] = "official unread · links failed"
        return empty
    website = str((links or {}).get("website") or "")
    handle = handle_from_url(str((links or {}).get("twitter") or ""))
    if not handle:
        return {
            **empty,
            "ok": True,
            "cred": "none",
            "line": "official none · no listed X",
        }
    user = await lookup_user(handle)
    if not user.get("ok"):
        if user.get("dead"):
            return {
                **empty,
                "ok": True,
                "cred": "dead",
                "handle": handle,
                "line": f"official dead · @{handle}",
                "error": user.get("error") or "",
            }
        empty["handle"] = handle
        empty["error"] = str(user.get("error") or "")
        empty["line"] = f"official unread · @{handle}"
        return empty
    age = _age_days(str(user.get("created_at") or ""))
    followers = int(user.get("followers") or 0)
    vtype = str(user.get("verified_type") or "")
    text = f"{user.get('name') or ''} {user.get('description') or ''}"
    site_match = None
    site_host = _domain(website)
    acct_host = _domain(str(user.get("url") or ""))
    if site_host and acct_host:
        site_match = site_host == acct_host or site_host.endswith("." + acct_host) or acct_host.endswith("." + site_host)
    mismatch = _mismatch(text, token, address)
    bio_hit = bool(token) and token.lstrip("$").lower() in text.lower()
    gold = vtype in ("business", "government")
    if mismatch:
        cred = "mismatch"
    elif gold or (
        age is not None and age >= 30 and followers >= 1000 and (site_match or bio_hit)
    ):
        cred = "high"
    elif age is not None and age >= 7 and followers >= 200 and (bio_hit or site_match or user.get("verified")):
        cred = "ok"
    else:
        cred = "thin"
    age_s = f"{age:.0f}d" if age is not None else "?d"
    bits = [f"official {cred}", f"@{user.get('username') or handle}", age_s, f"{_compact(followers)} followers"]
    if gold or vtype:
        bits.append(vtype or "verified")
    if site_match is True:
        bits.append("site match")
    elif site_match is False:
        bits.append("site mismatch")
    return {
        "ok": True,
        "cred": cred,
        "handle": str(user.get("username") or handle),
        "age_days": None if age is None else round(age, 1),
        "followers": followers,
        "verified_type": vtype,
        "site_match": site_match,
        "line": " · ".join(bits),
        "error": "",
        "description": str(user.get("description") or "")[:180],
    }


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
