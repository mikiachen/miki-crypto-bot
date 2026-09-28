"""Pump.fun read-only intel for the RH desk.

Solana narrative / hot-launch radar only. Never buys, never sells, never
imports crypto_executor. True fills stay on Robinhood Chain Uniswap (4663).

Reuses grok-trading-desk Scout create-stream + optional Narrative / local theme.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_GTD = _ROOT / "grok-trading-desk"
_VENDOR = _GTD / ".vendor"
for p in (_VENDOR, _ROOT, _GTD):
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

from desk_realtime.secrets import redact_text, sanitize_exc

log = logging.getLogger("pump_intel")

LOG_DIR = _GTD / "logs"
STATE_PATH = LOG_DIR / "pump_intel.json"
JSONL_PATH = LOG_DIR / "pump_intel.jsonl"
DESK_JSONL = LOG_DIR / "desk.jsonl"

# Hard rule: this module must never touch Solana execution.
_FORBIDDEN = ("crypto_executor", "CryptoExecutor", "execute_buy", "submit_buy")

_THEME_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ai", re.compile(r"\b(ai|gpt|claude|llm|agent|neural|robot)\b", re.I)),
    ("animal", re.compile(r"\b(dog|cat|pepe|frog|whale|bear|bull|monkey|ape)\b", re.I)),
    ("politics", re.compile(r"\b(trump|maga|biden|vote|election|elon)\b", re.I)),
    ("celeb", re.compile(r"\b(musk|trump|kanye|taylor|drake)\b", re.I)),
    ("finance", re.compile(r"\b(bank|fed|rates|gold|btc|eth|sol|usd)\b", re.I)),
    ("gaming", re.compile(r"\b(game|play|steam|xbox|nft|meta)\b", re.I)),
    ("meme", re.compile(r"\b(meme|chad|wojak|based|sigma)\b", re.I)),
)


def enabled() -> bool:
    return os.environ.get("PUMP_INTEL", "1").strip().lower() not in ("0", "false", "off", "no")


def narrative_llm() -> bool:
    return os.environ.get("PUMP_NARRATIVE_LLM", "0").strip().lower() in ("1", "true", "yes", "on")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    safe = {k: redact_text(v) if isinstance(v, str) else v for k, v in row.items()}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(safe, ensure_ascii=False) + "\n")


def read_intel() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return {"ok": False, "themes": [], "hot": [], "mode": "read_only"}
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {"ok": False, "themes": [], "hot": []}
    except Exception:
        return {"ok": False, "themes": [], "hot": []}


def theme_line(limit: int = 4) -> str:
    """One-line summary for RH desk SCAN feed."""
    st = read_intel()
    themes = st.get("themes") or []
    if not themes:
        age = float(st.get("updated_at") or 0)
        if age and time.time() - age < 120:
            return "pump quiet · no clustered themes"
        return "pump intel unread"
    bits = []
    for row in themes[:limit]:
        bits.append(f"{row.get('theme')}×{int(row.get('count') or 0)}")
    hot = st.get("hot") or []
    if hot:
        top = hot[0]
        bits.append(f"hot ${top.get('symbol') or '?'} {float(top.get('meme_score') or 0):.2f}")
    return "pump · " + " · ".join(bits)


def local_theme(symbol: str, name: str) -> str:
    blob = f"{symbol} {name}"
    for label, pat in _THEME_RULES:
        if pat.search(blob):
            return label
    # fallback: first alpha token of name
    parts = re.findall(r"[A-Za-z]{3,}", name or symbol or "")
    return (parts[0].lower() if parts else "misc")[:16]


def local_meme_score(symbol: str, name: str, *, socials: dict[str, Any] | None = None) -> float:
    score = 0.35
    text = f"{symbol} {name}".strip()
    if 2 <= len(symbol or "") <= 8:
        score += 0.12
    if socials:
        score += min(0.2, 0.07 * len(socials))
    theme = local_theme(symbol, name)
    if theme not in ("misc",):
        score += 0.1
    if re.search(r"(inu|pepe|dog|cat|ai|gpt)", text, re.I):
        score += 0.08
    return round(max(0.0, min(0.95, score)), 3)


def _env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _default_config() -> dict[str, Any]:
    # Default ON: short watch window + capped concurrency (radar + tape).
    watch_on = _env_bool("PUMP_WATCH", "1")
    return {
        "pump_fun": {
            "ws_url": os.environ.get("PUMP_WS_URL", "wss://pumpportal.fun/api/data"),
            "api_key": os.environ.get("PUMP_API_KEY", ""),
            "sol_price_usd": float(os.environ.get("PUMP_SOL_USD", "150") or 150),
            "watch": {
                "enabled": watch_on,
                "window_seconds": float(os.environ.get("PUMP_WATCH_SEC", "75")),
                "max_concurrent": int(os.environ.get("PUMP_WATCH_MAX", "12")),
                "min_trades_to_score": int(os.environ.get("PUMP_MIN_TRADES", "10")),
            },
        },
        "crypto_launch_filter": {
            "min_curve_sol": float(os.environ.get("PUMP_MIN_CURVE", "8")),
            "max_curve_sol": float(os.environ.get("PUMP_MAX_CURVE", "250")),
            "min_market_cap_sol": float(os.environ.get("PUMP_MIN_MCAP", "12")),
            "max_market_cap_sol": float(os.environ.get("PUMP_MAX_MCAP", "3000")),
            "max_dev_initial_buy_sol": float(os.environ.get("PUMP_MAX_DEV_BUY", "3")),
            "require_metadata": False,
            "require_socials": False,
        },
        # Stage-two tape gates (only applied when watch matures a token).
        "crypto_filter": {
            "min_buys": int(os.environ.get("PUMP_MIN_BUYS", "8")),
            "min_buy_sell_ratio": float(os.environ.get("PUMP_MIN_BSR", "1.15")),
            "min_unique_traders": int(os.environ.get("PUMP_MIN_TRADERS", "8")),
            "min_age_seconds": float(os.environ.get("PUMP_MIN_AGE", "30")),
            "max_age_seconds": float(os.environ.get("PUMP_MAX_AGE", "900")),
        },
    }


class PumpIntel:
    """Create-stream listener → theme board. No Solana execution path."""

    def __init__(self) -> None:
        self.config = _default_config()
        self.launches = 0
        self.passed = 0
        self.theme_counts: Counter[str] = Counter()
        self.hot: list[dict[str, Any]] = []
        self.recent: list[dict[str, Any]] = []
        self._seen_mints: set[str] = set()
        self._last_flush = 0.0

    def _record(self, row: dict[str, Any]) -> None:
        self.recent.append(row)
        self.recent = self.recent[-80:]
        theme = str(row.get("theme") or "misc")
        self.theme_counts[theme] += 1
        self.hot.append(row)
        self.hot.sort(key=lambda r: float(r.get("meme_score") or 0), reverse=True)
        # Dedupe by mint, keep top scores.
        seen: set[str] = set()
        uniq: list[dict[str, Any]] = []
        for item in self.hot:
            mint = str(item.get("mint") or "")
            if mint in seen:
                continue
            seen.add(mint)
            uniq.append(item)
            if len(uniq) >= 12:
                break
        self.hot = uniq
        _append_jsonl(JSONL_PATH, {"ts": _now(), **row})
        self.flush(force=False)

    def flush(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_flush < 8:
            return
        self._last_flush = now
        themes = [
            {"theme": t, "count": int(n)}
            for t, n in self.theme_counts.most_common(8)
        ]
        payload = {
            "ok": True,
            "mode": "read_only",
            "chain": "solana",
            "executes": False,
            "updated_at": now,
            "launches_seen": self.launches,
            "launches_passed": self.passed,
            "themes": themes,
            "hot": self.hot[:8],
            "recent": self.recent[-12:],
            "watch_default": True,
            "note": "intel only · radar+tape · RH Uniswap remains the only fill path",
        }
        _atomic_write(STATE_PATH, payload)

    async def _score(self, token: Any, *, watched: bool) -> dict[str, Any]:
        symbol = str(getattr(token, "symbol", "") or "?")
        name = str(getattr(token, "name", "") or "")
        mint = str(getattr(token, "mint", "") or "")
        socials = getattr(token, "socials", None) or {}
        theme = local_theme(symbol, name)
        meme = local_meme_score(symbol, name, socials=socials if isinstance(socials, dict) else {})
        source = "local+tape" if watched else "local"

        buys = int(getattr(token, "buys", 0) or 0)
        sells = int(getattr(token, "sells", 0) or 0)
        traders = int(getattr(token, "unique_traders", 0) or 0)
        try:
            bsr = float(token.buy_sell_ratio)  # type: ignore[attr-defined]
        except Exception:
            bsr = (buys / sells) if sells else float(buys)
        chg = float(getattr(token, "price_change_pct", 0) or 0)
        vol = float(getattr(token, "volume_sol", 0) or 0)

        if watched:
            # Tape heat: real flow beats name-only meme prior.
            if traders >= 12:
                meme += 0.08
            if bsr >= 1.4:
                meme += 0.07
            elif bsr < 0.9:
                meme -= 0.12
            if chg >= 15:
                meme += 0.05
            elif chg <= -20:
                meme -= 0.1
            if vol >= 15:
                meme += 0.04
            meme = round(max(0.05, min(0.98, meme)), 3)

        if narrative_llm():
            try:
                from desk_realtime.agent_scoring import score_narrative

                scored = await asyncio.wait_for(
                    score_narrative(symbol, mint),
                    timeout=float(os.environ.get("PUMP_NARR_TIMEOUT", "12")),
                )
                if scored.get("ok"):
                    # Blend model with tape-adjusted prior.
                    meme = round(0.55 * float(scored.get("score") or meme) + 0.45 * meme, 3)
                    source = f"{scored.get('source') or 'llm'}+tape" if watched else str(scored.get("source") or "llm")
            except Exception as exc:  # noqa: BLE001
                log.info("narrative llm skipped: %s", sanitize_exc(exc))

        return {
            "symbol": symbol[:16],
            "name": name[:48],
            "mint": mint,
            "theme": theme,
            "meme_score": round(meme, 3),
            "source": source,
            "watched": watched,
            "buys": buys,
            "sells": sells,
            "unique_traders": traders,
            "buy_sell_ratio": round(bsr, 3),
            "price_change_pct": round(chg, 2),
            "volume_sol": round(vol, 3),
            "curve_sol": float(getattr(token, "curve_sol", 0) or 0),
            "market_cap_sol": float(getattr(token, "market_cap_sol", 0) or 0),
            "socials": list(socials.keys()) if isinstance(socials, dict) else [],
            "ts": _now(),
        }

    async def run(self) -> None:
        # Guard: never allow executor import via env tricks in this process.
        for name in _FORBIDDEN:
            if name in sys.modules:
                raise RuntimeError(f"pump_intel refused · {name} already loaded")

        from src.crypto.scout import Scout

        scout = Scout(self.config)
        watched = bool(scout.watch_enabled)
        log.info(
            "pump intel armed · read_only · watch=%s window=%.0fs max=%d · narrative_llm=%s",
            watched,
            float(scout.window_seconds),
            int(scout.max_concurrent),
            narrative_llm(),
        )
        self.flush(force=True)

        # Prefer Scout.stream: create → short trade watch → mature survivors.
        # executes=false always; stream only yields Token objects, never fills.
        while True:
            try:
                async for token in scout.stream():
                    self.launches += 1
                    mint = str(getattr(token, "mint", "") or "")
                    if not mint or mint in self._seen_mints:
                        continue
                    self._seen_mints.add(mint)
                    if len(self._seen_mints) > 5000:
                        self._seen_mints = set(list(self._seen_mints)[-2500:])
                    self.passed += 1
                    row = await self._score(token, watched=watched)
                    self._record(row)
                    log.info(
                        "intel $%s · theme %s · meme %.2f · b/s %s/%s · traders %s · %s",
                        row["symbol"],
                        row["theme"],
                        row["meme_score"],
                        row.get("buys"),
                        row.get("sells"),
                        row.get("unique_traders"),
                        row["source"],
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("pump stream restart: %s", sanitize_exc(exc))
                self.flush(force=True)
                await asyncio.sleep(3.0)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    p = argparse.ArgumentParser(description="Pump.fun read-only narrative intel (no Solana trades)")
    p.add_argument("--once-status", action="store_true", help="print current intel file and exit")
    args = p.parse_args()
    if args.once_status:
        print(json.dumps(read_intel(), indent=2))
        return
    if not enabled():
        log.info("PUMP_INTEL=0 · idle")
        return
    # Explicit: no live Solana path in this process.
    os.environ["PUMP_EXEC"] = "0"
    os.environ.setdefault("DESK_CHAIN", "robinhood")
    asyncio.run(PumpIntel().run())


if __name__ == "__main__":
    main()
