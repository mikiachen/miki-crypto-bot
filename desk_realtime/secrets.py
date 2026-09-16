"""Secret redaction — never leak keys/signers into Desk Feed, logs, or WS."""

from __future__ import annotations

import os
import re
from typing import Any

# Patterns that must never reach UI / jsonl / stdout
_SECRET_KEYS = frozenset({
    "wallet_key", "private_key", "secret", "api_key", "api_secret",
    "secret_key", "signing_key", "mnemonic", "seed", "passphrase",
    "authorization", "bearer", "arc_private_key", "solana_wallet_key",
})

# Base58-ish blobs, hex keys, sk-/xai-/pk prefixes
_SECRET_RE = re.compile(
    r"(?i)("
    r"(?:--|/)private-key(?:\s+|=)\s*\S+"
    r"|(?:wallet[_-]?key|private[_-]?key|api[_-]?key|api[_-]?secret|secret[_-]?key|ARC_PRIVATE_KEY)"
    r"\s*[:=]\s*['\"]?[A-Za-z0-9_\-+/=]{16,}"
    r"|sk-(?:or-)?[A-Za-z0-9_\-]{20,}"
    r"|xai-[A-Za-z0-9_\-]{20,}"
    r"|Bearer\s+[A-Za-z0-9\-._~+/]+=*"
    r"|[1-9A-HJ-NP-Za-km-z]{64,88}"  # base58 secret-key sized
    r"|0x[a-fA-F0-9]{64}"             # 0x-prefixed EVM private key
    r"|(?<![A-Za-z0-9])[a-fA-F0-9]{64}(?![A-Za-z0-9])"  # bare 32-byte key
    r")"
)

_REDACTED = "[REDACTED]"


def is_secret_key_name(name: str) -> bool:
    n = str(name or "").strip().lower().replace("-", "_")
    if n in _SECRET_KEYS:
        return True
    return any(s in n for s in ("private", "secret", "mnemonic", "seed_phrase", "wallet_key"))


def redact_text(text: Any) -> str:
    """Strip secret-looking substrings from an arbitrary message."""
    s = str(text if text is not None else "")
    if not s:
        return s
    try:
        return _SECRET_RE.sub(_REDACTED, s)
    except Exception:
        return "[redact-error]"


def sanitize_exc(exc: BaseException) -> str:
    """Safe one-line error for Desk Feed / metrics (no stack, no secrets)."""
    name = type(exc).__name__
    msg = redact_text(exc)
    # Drop traceback-ish / path dumps that may embed env
    msg = re.sub(r"(?i)(/Users/[^\s]+|/home/[^\s]+|C:\\\\[^\s]+)", "[PATH]", msg)
    msg = msg.replace("\n", " ").strip()
    if len(msg) > 160:
        msg = msg[:157] + "…"
    return f"{name}: {msg}" if msg else name


def scrub_mapping(data: dict[str, Any] | None) -> dict[str, Any]:
    """Return a shallow-safe copy with secret fields removed/redacted."""
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in data.items():
        if is_secret_key_name(str(k)):
            out[str(k)] = _REDACTED
        elif isinstance(v, dict):
            out[str(k)] = scrub_mapping(v)
        elif isinstance(v, str):
            out[str(k)] = redact_text(v)
        else:
            out[str(k)] = v
    return out


def load_wallet_key_from_env_or_config(cfg: dict[str, Any] | None) -> str:
    """
    Resolve Solana wallet secret from ENV first (preferred), else config.
    Never log the return value.
    """
    env = (
        os.environ.get("SOLANA_WALLET_KEY")
        or os.environ.get("SOLANA_PRIVATE_KEY")
        or ""
    ).strip()
    if env and env not in ("REPLACE_ME_BASE58_SECRET_KEY", "DRY_RUN_NO_KEY"):
        return env
    sol = (cfg or {}).get("solana") or {}
    key = str(sol.get("wallet_key") or "").strip()
    if key in ("", "REPLACE_ME_BASE58_SECRET_KEY", "DRY_RUN_NO_KEY"):
        return ""
    return key
