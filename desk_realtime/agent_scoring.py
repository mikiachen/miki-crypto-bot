"""Async agent scoring with hard timeouts — never blocks the trading loop forever."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from pathlib import Path
from typing import Any

import yaml

from desk_realtime.async_http import llm_chat_json
from desk_realtime.fastforward import async_pause
from desk_realtime.llm_budget import allow_llm_call, charge_llm_call
from desk_realtime.secrets import redact_text, sanitize_exc

log = logging.getLogger("agent_scoring")

_ROOT = Path(__file__).resolve().parents[1]
_CFG = _ROOT / "grok-trading-desk" / "config.yaml"

SCORE_TIMEOUT = float(os.environ.get("AGENT_SCORE_TIMEOUT", "20"))
_NARR_CACHE: dict[str, dict[str, Any]] = {}


def _narr_cache_key(token: str, address: str) -> str:
    return (address or token or "").strip().lower()


def _narr_cache_get(key: str) -> dict[str, Any] | None:
    if not key:
        return None
    row = _NARR_CACHE.get(key)
    if not row:
        return None
    ttl = float(os.environ.get("NARRATIVE_CACHE_SEC", "900"))
    if time.time() - float(row.get("ts") or 0) > ttl:
        _NARR_CACHE.pop(key, None)
        return None
    data = dict(row.get("data") or {})
    data["cached"] = True
    return data


def _narr_cache_put(key: str, data: dict[str, Any]) -> None:
    if not key:
        return
    _NARR_CACHE[key] = {"ts": time.time(), "data": dict(data)}


def _cfg() -> dict[str, Any]:
    if not _CFG.is_file():
        return {}
    try:
        raw = yaml.safe_load(_CFG.read_text(encoding="utf-8")) or {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def resolve_llm_endpoint() -> dict[str, str]:
    """
    Prefer OpenRouter-compatible creds already in .env:
      ANTHROPIC_API_KEY + ANTHROPIC_BASE_URL  (sk-or-v1… → openrouter)
      or OPENROUTER_API_KEY / GROK_API_KEY
    """
    cfg = _cfg()
    grok = cfg.get("grok") or {}
    key = str(
        os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("GROK_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or grok.get("api_key")
        or ""
    ).strip()
    # Ignore placeholders
    if key in ("sk-or-...", "REPLACE_ME_OPENROUTER", "") or key.startswith("REPLACE"):
        key = str(os.environ.get("ANTHROPIC_API_KEY") or "").strip()

    base = str(
        os.environ.get("OPENROUTER_BASE_URL")
        or os.environ.get("ANTHROPIC_BASE_URL")
        or grok.get("base_url")
        or "https://openrouter.ai/api/v1"
    ).strip().rstrip("/")

    # Normalize to .../chat/completions exactly once (avoid /v1/chat/completions/chat/completions)
    if base.endswith("/chat/completions"):
        pass
    elif base.endswith("/api/v1") or base.endswith("/v1"):
        base = base + "/chat/completions"
    elif "openrouter.ai" in base:
        base = "https://openrouter.ai/api/v1/chat/completions"
    else:
        base = base + "/chat/completions"

    model = str(
        os.environ.get("AI_QUANT_LAB_MODEL")
        or os.environ.get("OPENROUTER_MODEL")
        or (grok.get("models") or {}).get("fast")
        # Default to a widely available OpenRouter id (stale sonnet slugs → 404)
        or "openai/gpt-4o-mini"
    )
    return {"api_key": key, "base_url": base, "model": model}


async def score_narrative(
    token: str,
    address: str,
    *,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """
    Narrative / theme match score in [0,1].
    Blends X (Twitter) recent-search pulse when X_BEARER_TOKEN is set,
    then LLM when AGENT_LLM=1 + key + budget allows; else local_sim.
    """
    r = rng or random.Random()
    cache_key = _narr_cache_key(token, address)
    cached = _narr_cache_get(cache_key)
    if cached:
        return cached

    # —— Real-time X pulse (optional) ——
    x_pulse: dict[str, Any] = {}
    try:
        from desk_realtime.x_client import narrative_x_pulse, x_api_enabled

        if x_api_enabled():
            x_pulse = await asyncio.wait_for(
                narrative_x_pulse(token, address),
                timeout=float(os.environ.get("X_SEARCH_TIMEOUT", "8")),
            )
    except Exception as exc:  # noqa: BLE001
        log.info("X pulse skipped: %s", sanitize_exc(exc))
        x_pulse = {"ok": False, "error": sanitize_exc(exc)}

    def _live_book() -> bool:
        net = os.environ.get("ARC_NETWORK", "").strip().lower()
        live = os.environ.get("ARC_LIVE", "").strip().lower() in ("1", "true", "yes", "on")
        return net == "mainnet" or live

    async def _closed(reason: str) -> dict[str, Any]:
        """Mainnet never invents a passing narrative score."""
        await async_pause(0.01)
        return {
            "ok": False,
            "score": 0.0,
            "off_narrative": True,
            "source": reason,
            "token": token,
            "token_address": address,
            "x_mentions": x_pulse.get("mention_count"),
            "x_engagement": x_pulse.get("engagement"),
        }

    async def _local(reason: str = "local_sim") -> dict[str, Any]:
        if _live_book():
            return await _closed(reason)
        await async_pause(0.05)
        score = round(r.uniform(0.42, 0.98), 2)
        if x_pulse.get("ok") and x_pulse.get("score_hint") is not None:
            # Paper only: 60% X heat + 40% local prior
            score = round(0.6 * float(x_pulse["score_hint"]) + 0.4 * score, 2)
        return {
            "ok": True,
            "score": score,
            "off_narrative": score < 0.62,
            "source": reason if not x_pulse.get("ok") else f"{reason}+x",
            "token": token,
            "token_address": address,
            "x_mentions": x_pulse.get("mention_count"),
            "x_engagement": x_pulse.get("engagement"),
        }

    async def _llm() -> dict[str, Any]:
        ep = resolve_llm_endpoint()
        key, base, model = ep["api_key"], ep["base_url"], ep["model"]
        if not key:
            raise RuntimeError("no LLM api key (set ANTHROPIC_API_KEY or OPENROUTER_API_KEY)")
        cfg = _cfg()
        grok = cfg.get("grok") or {}
        timeout = float(grok.get("timeout_seconds") or SCORE_TIMEOUT)
        x_ctx = ""
        if x_pulse.get("ok"):
            samples = x_pulse.get("samples") or []
            x_ctx = (
                f" X_mentions={x_pulse.get('mention_count')} "
                f"engagement={x_pulse.get('engagement')} "
                f"samples={samples[:2]}"
            )
        official = x_pulse.get("official") if isinstance(x_pulse.get("official"), dict) else {}
        if official.get("line"):
            x_ctx += f" official={official.get('line')}"
        messages = [
            {
                "role": "system",
                "content": (
                    "Return ONLY JSON {\"score\":0-1,\"off_narrative\":bool,\"note\":str} "
                    "for an Arc Network meme launch narrative fit (USDC-native L1). "
                    "Weight live X mention heat when provided. No secrets."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"token=${x_pulse.get('symbol') or token} "
                    f"address={x_pulse.get('token') or address} "
                    f"chain=arc{x_ctx}"
                ),
            },
        ]
        data = await llm_chat_json(
            base_url=base,
            api_key=key,
            model=model,
            messages=messages,
            timeout=timeout,
        )
        score = float(data.get("score", 0.5))
        score = max(0.0, min(1.0, score))
        if x_pulse.get("ok") and x_pulse.get("score_hint") is not None:
            score = max(0.0, min(1.0, 0.7 * score + 0.3 * float(x_pulse["score_hint"])))
        # Model may flag a hostile story. 0.62 is not a narrative veto.
        off = bool(data.get("off_narrative")) and score < 0.25
        charge_llm_call()
        return {
            "ok": True,
            "score": round(score, 2),
            "off_narrative": off,
            "source": "llm+x" if x_pulse.get("ok") else "llm",
            "note": redact_text(data.get("note") or ""),
            "token": token,
            "token_address": address,
            "model": model,
            "x_mentions": x_pulse.get("mention_count"),
            "x_engagement": x_pulse.get("engagement"),
        }

    try:
        from desk_realtime.x_client import official_account, x_api_enabled as _x_on

        if _x_on() and not isinstance(x_pulse.get("official"), dict):
            official = await asyncio.wait_for(
                official_account(token, address),
                timeout=float(os.environ.get("X_SEARCH_TIMEOUT", "8")),
            )
            x_pulse = dict(x_pulse)
            x_pulse["official"] = official
            if official.get("cred") == "mismatch":
                scored = {
                    "ok": True,
                    "score": 0.0,
                    "off_narrative": True,
                    "source": "official_mismatch",
                    "token": token,
                    "token_address": address,
                    "x_mentions": x_pulse.get("mention_count"),
                    "x_engagement": x_pulse.get("engagement"),
                    "official": official,
                    "note": official.get("line") or "official mismatch",
                }
                _narr_cache_put(cache_key, scored)
                return scored
    except Exception as exc:  # noqa: BLE001
        log.info("official X skipped: %s", sanitize_exc(exc))

    # AGENT_LLM may be glued as "1# comment" in .env — take leading token
    raw_flag = os.environ.get("AGENT_LLM", "").strip().split("#", 1)[0].strip()
    use_llm = raw_flag.lower() in ("1", "true", "yes", "on")

    try:
        if use_llm:
            min_x = int(os.environ.get("LLM_MIN_X_MENTIONS", "3"))
            mentions = int(x_pulse.get("mention_count") or 0)
            if mentions < min_x:
                # Save the Grok call. Do not fail-close — Arc book score uses the tape.
                log.info("LLM skip · cold x mentions=%s <%s · $%s", mentions, min_x, token)
                hint = x_pulse.get("score_hint")
                scored = {
                    "ok": True,
                    "score": round(float(hint), 2) if hint is not None else 0.0,
                    "off_narrative": False,
                    "source": "cold_x",
                    "token": token,
                    "token_address": address,
                    "x_mentions": mentions,
                    "x_engagement": x_pulse.get("engagement"),
                    "official": x_pulse.get("official"),
                    "note": (x_pulse.get("official") or {}).get("line") if isinstance(x_pulse.get("official"), dict) else "",
                }
                _narr_cache_put(cache_key, scored)
                return scored
            ok, reason = allow_llm_call()
            if not ok:
                log.info("LLM cut · %s · fallback local_sim", reason)
                scored = await asyncio.wait_for(
                    _local(f"budget_cut:{reason}"),
                    timeout=min(SCORE_TIMEOUT, 5.0),
                )
                _narr_cache_put(cache_key, scored)
                return scored
            try:
                scored = await asyncio.wait_for(_llm(), timeout=SCORE_TIMEOUT)
                _narr_cache_put(cache_key, scored)
                return scored
            except Exception as llm_exc:  # noqa: BLE001
                # OpenRouter/model 404 etc. — keep desk running on X + local
                log.warning("LLM narrative failed, fallback local+x: %s", sanitize_exc(llm_exc))
                scored = await asyncio.wait_for(
                    _local(f"llm_fallback:{sanitize_exc(llm_exc)[:40]}"),
                    timeout=min(SCORE_TIMEOUT, 8.0),
                )
                _narr_cache_put(cache_key, scored)
                return scored
        scored = await asyncio.wait_for(_local(), timeout=min(SCORE_TIMEOUT, 12.0))
        _narr_cache_put(cache_key, scored)
        return scored
    except asyncio.TimeoutError:
        log.warning("narrative score timeout — fail closed")
        return {
            "ok": False,
            "score": 0.0,
            "off_narrative": True,
            "source": "timeout",
            "token": token,
            "error": "score timeout",
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("narrative score error: %s", sanitize_exc(exc))
        try:
            return await _local(f"error_fallback:{sanitize_exc(exc)[:40]}")
        except Exception:
            return {
                "ok": False,
                "score": 0.0,
                "off_narrative": True,
                "source": "error",
                "token": token,
                "error": sanitize_exc(exc),
            }


async def scan_story(token: str, address: str) -> dict[str, Any]:
    """Mention heat plus listed official account. Does not buy or veto a quiet tape."""
    from desk_realtime.x_client import narrative_x_pulse, official_account, x_api_enabled

    pulse: dict[str, Any] = {}
    official: dict[str, Any] = {"cred": "unread", "line": "official unread · X off"}
    if x_api_enabled():
        pulse_r, official_r = await asyncio.gather(
            narrative_x_pulse(token, address),
            official_account(token, address),
            return_exceptions=True,
        )
        if isinstance(pulse_r, dict):
            pulse = pulse_r
        else:
            log.info("X pulse skipped: %s", sanitize_exc(pulse_r))
        if isinstance(official_r, dict):
            official = official_r
        else:
            log.info("official X skipped: %s", sanitize_exc(official_r))
            official = {"cred": "unread", "line": "official unread"}
    hint = pulse.get("score_hint")
    mismatch = official.get("cred") == "mismatch"
    return {
        "ok": True,
        "score": round(float(hint), 2) if hint is not None else 0.0,
        "off_narrative": mismatch,
        "source": "x_scan" if pulse.get("ok") else "tape+x",
        "token": token,
        "token_address": address,
        "x_mentions": pulse.get("mention_count"),
        "x_engagement": pulse.get("engagement"),
        "official": official,
        "note": official.get("line") or "",
    }
