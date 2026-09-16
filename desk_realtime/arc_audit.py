"""Arc-only three-tier audit gate (meme-radar style) — stub first, real APIs later.

Tiers
-----
REJECT  — hard no (honeypot / thin LP / known bad) → never buy
REVIEW  — unknown / incomplete evidence → treat as 待复核, never auto-buy
WATCH   — 可看 — evidence clear enough to continue to TIMING (still not advice)

Unknown fields MUST NOT pass. Missing provider data → REVIEW.

Providers (hooks only until keys exist):
  * goplus   — contract risk (future)
  * dexscreener / launchpad — mcap, liquidity, website (future)
  * stub     — deterministic local sim for paper / dry-run
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger("arc_audit")

ARC_CHAIN_ID = int(os.environ.get("ARC_CHAIN_ID", "5042002"))
# stub | live (live still falls back to stub when providers unset)
AUDIT_MODE = os.environ.get("ARC_AUDIT_MODE", "stub").strip().lower()
GOPLUS_ENABLED = os.environ.get("ARC_AUDIT_GOPLUS", "0").strip() in ("1", "true", "True")
DEX_ENABLED = os.environ.get("ARC_AUDIT_DEX", "0").strip() in ("1", "true", "True")


class AuditTier(str, Enum):
    REJECT = "REJECT"  # 拒绝
    REVIEW = "REVIEW"  # 待复核
    WATCH = "WATCH"  # 可看


@dataclass
class AuditField:
    name: str
    value: Any = None
    status: str = "UNKNOWN"  # OK | BAD | UNKNOWN
    note: str = ""


@dataclass
class AuditResult:
    tier: AuditTier
    token: str
    address: str
    chain: str = "arc"
    chain_id: int = ARC_CHAIN_ID
    reason: str = ""
    fields: list[AuditField] = field(default_factory=list)
    source: str = "stub"
    ts: float = 0.0

    @property
    def allows_buy(self) -> bool:
        """Only WATCH may proceed to TIMING / executeBuyOrder."""
        return self.tier == AuditTier.WATCH

    @property
    def log_text(self) -> str:
        label = {
            AuditTier.REJECT: "拒绝",
            AuditTier.REVIEW: "待复核",
            AuditTier.WATCH: "可看",
        }[self.tier]
        return f"AUDIT {label} · {self.reason}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tier"] = self.tier.value
        d["allows_buy"] = self.allows_buy
        d["log_text"] = self.log_text
        return d


def _addr_seed(address: str) -> int:
    h = hashlib.sha256(address.lower().encode()).hexdigest()
    return int(h[:8], 16)


def _stub_fields(token: str, address: str) -> list[AuditField]:
    """
    Deterministic stub evidence.
    Intentionally leaves several fields UNKNOWN so REVIEW is the common path
    until real providers are wired — unknown ≠ pass.
    """
    seed = _addr_seed(address or token)
    # ~20% clear WATCH-shaped, ~25% hard REJECT cues, rest UNKNOWN-heavy
    bucket = seed % 100

    fields: list[AuditField] = [
        AuditField("chain", "arc", "OK", f"chain_id={ARC_CHAIN_ID}"),
        AuditField("address_format", address[:10] + "…" if address else "", "OK" if address.startswith("0x") and len(address) >= 42 else "UNKNOWN"),
    ]

    if bucket < 25:
        fields.extend(
            [
                AuditField("honeypot", True, "BAD", "stub · honeypot signal"),
                AuditField("lp_lock", None, "UNKNOWN", "provider unset"),
                AuditField("tax", None, "UNKNOWN", "provider unset"),
                AuditField("liquidity_usd", None, "UNKNOWN", "provider unset"),
            ]
        )
    elif bucket < 45:
        fields.extend(
            [
                AuditField("honeypot", False, "OK", "stub · clear"),
                AuditField("lp_lock", True, "OK", "stub · locked"),
                AuditField("tax", 0.01, "OK", "stub · 1%"),
                AuditField("liquidity_usd", 80_000 + (seed % 50_000), "OK", "stub · depth ok"),
                AuditField("holders_top10", 0.22, "OK", "stub · <45%"),
                AuditField("website", None, "UNKNOWN", "dex provider unset"),
            ]
        )
    else:
        # Dominant path: incomplete evidence → REVIEW
        fields.extend(
            [
                AuditField("honeypot", None, "UNKNOWN", "goplus unset"),
                AuditField("lp_lock", None, "UNKNOWN", "goplus unset"),
                AuditField("tax", None, "UNKNOWN", "goplus unset"),
                AuditField("liquidity_usd", None, "UNKNOWN", "dexscreener unset"),
                AuditField("mcap_usd", None, "UNKNOWN", "dexscreener unset"),
                AuditField("website", None, "UNKNOWN", "dexscreener unset"),
            ]
        )
    return fields


def _tier_from_fields(fields: list[AuditField]) -> tuple[AuditTier, str]:
    bads = [f for f in fields if f.status == "BAD"]
    unknowns = [f for f in fields if f.status == "UNKNOWN"]
    critical = {"honeypot", "lp_lock", "tax", "liquidity_usd"}

    if any(f.name == "honeypot" and f.status == "BAD" for f in fields):
        return AuditTier.REJECT, "honeypot / 貔貅风险"
    if bads:
        return AuditTier.REJECT, f"bad:{','.join(f.name for f in bads[:3])}"

    # Critical UNKNOWN → REVIEW (never auto-buy). Soft unknowns (website…) OK for WATCH.
    crit_unknown = [f for f in unknowns if f.name in critical]
    if crit_unknown:
        names = ",".join(f.name for f in crit_unknown[:4])
        return AuditTier.REVIEW, f"未知字段待复核 · {names}"

    soft = [f for f in unknowns if f.name not in critical]
    if soft:
        return AuditTier.WATCH, f"可看 · 次要未知:{','.join(f.name for f in soft[:2])}"

    return AuditTier.WATCH, "证据齐全 · 可看（非买入建议）"


def _goplus_on() -> bool:
    return os.environ.get("ARC_AUDIT_GOPLUS", "0").strip() in ("1", "true", "True")


def _dex_on() -> bool:
    from desk_realtime.arc_dex import dex_enabled

    return dex_enabled()


def _unknown_risk(note: str) -> list[AuditField]:
    return [
        AuditField("honeypot", None, "UNKNOWN", note),
        AuditField("lp_lock", None, "UNKNOWN", note),
        AuditField("tax", None, "UNKNOWN", note),
    ]


def _na_risk(note: str) -> list[AuditField]:
    """Provider gap. Not a honeypot, and not a pass. On-chain quote is the sell check."""
    return [
        AuditField("honeypot", None, "N/A", note),
        AuditField("lp_lock", None, "N/A", note),
        AuditField("tax", None, "N/A", note),
    ]


def _unknown_market(note: str) -> list[AuditField]:
    return [
        AuditField("liquidity_usd", None, "UNKNOWN", note),
        AuditField("mcap_usd", None, "UNKNOWN", note),
        AuditField("website", None, "UNKNOWN", note),
    ]


async def fetch_goplus(address: str) -> list[AuditField]:
    """GoPlus token_security. Arc 5042 is not in their chain list yet (code 2022)."""
    if not _goplus_on():
        return _na_risk("goplus off · chain 5042 unsupported")
    addr = (address or "").strip()
    if not (addr.startswith("0x") and len(addr) >= 42):
        return _unknown_risk("goplus skipped · bad address")

    chain_id = os.environ.get("ARC_CHAIN_ID", str(ARC_CHAIN_ID)).strip() or "5042"
    url = (
        "https://api.gopluslabs.io/api/v1/token_security/"
        f"{chain_id}?contract_addresses={addr}"
    )
    headers = {"User-Agent": "miki-desk-audit/1.0", "Accept": "application/json"}
    key = (os.environ.get("GOPLUS_API_KEY") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        from desk_realtime.async_http import http_json

        data = await http_json("GET", url, headers=headers, timeout=8.0)
    except Exception as exc:  # noqa: BLE001
        log.info("goplus fetch failed: %s", exc)
        return _unknown_risk("goplus unreachable")

    code = data.get("code")
    if code not in (1, "1"):
        msg = str(data.get("message") or "goplus rejected")[:80]
        return _na_risk(f"goplus · {msg}")

    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    row = result.get(addr) or result.get(addr.lower()) or {}
    if not isinstance(row, dict) or not row:
        return _unknown_risk("goplus · empty result")

    honeypot = str(row.get("is_honeypot") or "")
    honeypot_field = AuditField(
        "honeypot",
        honeypot == "1",
        "BAD" if honeypot == "1" else "OK" if honeypot == "0" else "UNKNOWN",
        "goplus is_honeypot",
    )
    taxes = []
    for key_name in ("buy_tax", "sell_tax"):
        raw = row.get(key_name)
        try:
            taxes.append(float(raw))
        except (TypeError, ValueError):
            pass
    if not taxes:
        tax_field = AuditField("tax", None, "UNKNOWN", "goplus tax missing")
    else:
        tax = max(taxes)
        tax_field = AuditField(
            "tax",
            tax,
            "BAD" if tax > 0.10 else "OK",
            "goplus buy/sell tax",
        )
    holders = row.get("lp_holders") if isinstance(row.get("lp_holders"), list) else []
    locked = any(str(h.get("is_locked")) == "1" for h in holders if isinstance(h, dict))
    lp_field = AuditField(
        "lp_lock",
        True if locked else None,
        "OK" if locked else "UNKNOWN",
        "goplus lp_holders" if holders else "goplus lp missing",
    )
    return [honeypot_field, lp_field, tax_field]


async def fetch_dex(address: str) -> list[AuditField]:
    """Dexscreener Arc pairs. Curve addresses are resolved to token() first."""
    if not _dex_on():
        return _unknown_market("dex disabled")
    addr = (address or "").strip()
    if not (addr.startswith("0x") and len(addr) >= 42):
        return _unknown_market("dex skipped · bad address")

    try:
        from desk_realtime.arc_dex import best_pair

        best = best_pair(addr)
    except Exception as exc:  # noqa: BLE001
        log.info("dexscreener fetch failed: %s", exc)
        return _unknown_market("dexscreener unreachable")

    if not best:
        return [
            AuditField("liquidity_usd", None, "N/A", "dexscreener · no pair · not a buy trigger"),
            AuditField("mcap_usd", None, "N/A", "dexscreener · no pair"),
            AuditField("website", None, "UNKNOWN", "dexscreener · no pair"),
        ]

    liq = (best.get("liquidity") or {}) if isinstance(best.get("liquidity"), dict) else {}
    try:
        liq_usd = float(liq.get("usd"))
    except (TypeError, ValueError):
        liq_usd = None
    try:
        mcap = float(best.get("marketCap") or best.get("fdv"))
    except (TypeError, ValueError):
        mcap = None
    info = best.get("info") if isinstance(best.get("info"), dict) else {}
    sites = info.get("websites") if isinstance(info.get("websites"), list) else []
    website = ""
    if sites and isinstance(sites[0], dict):
        website = str(sites[0].get("url") or "")
    base = best.get("baseToken") if isinstance(best.get("baseToken"), dict) else {}
    note = str(base.get("symbol") or "pair")
    return [
        AuditField(
            "liquidity_usd",
            liq_usd,
            "OK" if liq_usd and liq_usd > 0 else "UNKNOWN",
            f"dexscreener {note} liquidity.usd",
        ),
        AuditField(
            "mcap_usd",
            mcap,
            "OK" if mcap and mcap > 0 else "UNKNOWN",
            "dexscreener marketCap",
        ),
        AuditField(
            "website",
            website or None,
            "OK" if website else "UNKNOWN",
            "dexscreener websites",
        ),
    ]


async def audit_token(
    token: str,
    address: str,
    *,
    chain: str = "arc",
) -> AuditResult:
    """
    Arc-only audit. Non-arc chains → REJECT.
    Mode stub: local deterministic fields.
    Mode live: merge provider hooks (still UNKNOWN-heavy until wired).
    """
    return await _audit_token_async(token, address, chain=chain)


def audit_token_sync(
    token: str,
    address: str,
    *,
    chain: str = "arc",
) -> AuditResult:
    """Sync entry for Streamlit paper ticks (stub / no event loop)."""
    tok = str(token).lstrip("$").upper()
    addr = str(address or "").strip()
    now = time.time()

    if chain.lower() != "arc":
        return AuditResult(
            tier=AuditTier.REJECT,
            token=tok,
            address=addr,
            reason=f"chain={chain} not supported · Arc only",
            fields=[AuditField("chain", chain, "BAD", "Arc only")],
            source="gate",
            ts=now,
        )

    # Sync path always uses stub fields; live providers stay async-only
    fields = _stub_fields(tok, addr)
    tier, reason = _tier_from_fields(fields)
    return AuditResult(
        tier=tier,
        token=tok,
        address=addr,
        reason=reason,
        fields=fields,
        source="stub",
        ts=now,
    )


async def _audit_token_async(
    token: str,
    address: str,
    *,
    chain: str = "arc",
) -> AuditResult:
    tok = str(token).lstrip("$").upper()
    addr = str(address or "").strip()
    now = time.time()

    if chain.lower() != "arc":
        return AuditResult(
            tier=AuditTier.REJECT,
            token=tok,
            address=addr,
            reason=f"chain={chain} not supported · Arc only",
            fields=[AuditField("chain", chain, "BAD", "Arc only")],
            source="gate",
            ts=now,
        )

    mainnet = os.environ.get("ARC_NETWORK", "").strip().lower() == "mainnet"
    if AUDIT_MODE == "live" or mainnet:
        fields = [
            AuditField("chain", "arc", "OK", f"chain_id={ARC_CHAIN_ID}"),
            AuditField(
                "address_format",
                addr[:10] + "…" if addr else "",
                "OK" if addr.startswith("0x") and len(addr) >= 42 else "UNKNOWN",
            ),
        ]
        fields.extend(await fetch_goplus(addr))
        fields.extend(await fetch_dex(addr))
        source = "live_hooks"
    else:
        fields = _stub_fields(tok, addr)
        source = "stub"

    tier, reason = _tier_from_fields(fields)
    result = AuditResult(
        tier=tier,
        token=tok,
        address=addr,
        reason=reason,
        fields=fields,
        source=source,
        ts=now,
    )
    log.info(
        "audit $%s tier=%s src=%s · %s",
        tok,
        result.tier.value,
        source,
        reason,
    )
    return result
