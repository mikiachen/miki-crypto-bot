"""Solana / pump.fun executor — async, dry-run by default, secrets never logged.

LIVE requires:
  SOLANA_LIVE=1
  SOLANA_WALLET_KEY (or config solana.wallet_key) — base58 secret
  reachable solana.rpc_url

Panic sell bypasses agent scoring and market-flattens the open book.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

import yaml

from desk_realtime.async_http import solana_rpc
from desk_realtime.secrets import (
    load_wallet_key_from_env_or_config,
    redact_text,
    sanitize_exc,
)

log = logging.getLogger("crypto_executor")

_ROOT = Path(__file__).resolve().parents[1]
_CFG_PATH = _ROOT / "grok-trading-desk" / "config.yaml"


def _load_cfg() -> dict[str, Any]:
    if not _CFG_PATH.is_file():
        return {}
    try:
        raw = yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8")) or {}
        return raw if isinstance(raw, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.warning("config load failed: %s", sanitize_exc(exc))
        return {}


class SolanaExecutor:
    """Buy / sell Solana memes. Default dry_run — no signing unless LIVE."""

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config if config is not None else _load_cfg()
        sol = (cfg.get("solana") or {}) if isinstance(cfg, dict) else {}
        self.rpc_url = str(
            os.environ.get("SOLANA_RPC_URL")
            or sol.get("rpc_url")
            or "https://api.mainnet-beta.solana.com"
        )
        self.slippage_bps = int(sol.get("slippage_bps") or 500)
        self.live = (
            os.environ.get("SOLANA_LIVE", "").strip() in ("1", "true", "True")
            or str(cfg.get("mode") or "").lower() == "live"
        )
        # Kept only in memory — never attached to emit/log payloads
        self._wallet_key = load_wallet_key_from_env_or_config(cfg)
        self.timeout = float(os.environ.get("SOLANA_RPC_TIMEOUT", "12"))

    def _has_signer(self) -> bool:
        return bool(self._wallet_key)

    async def health_check(self) -> dict[str, Any]:
        """Non-blocking RPC probe (getHealth / getSlot)."""
        try:
            slot = await solana_rpc(
                self.rpc_url, "getSlot", timeout=min(self.timeout, 8.0)
            )
            return {"ok": True, "slot": slot, "rpc": redact_text(self.rpc_url)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": sanitize_exc(exc)}

    async def buy(
        self,
        token_address: str,
        stake_sol: float,
        *,
        symbol: str = "",
    ) -> dict[str, Any]:
        """Market-style buy for `stake_sol`. Dry-run unless LIVE + signer."""
        mint = str(token_address or "").strip()
        if not mint:
            return {"ok": False, "error": "missing token_address", "dry_run": True}

        if not self.live or not self._has_signer():
            # Still touch RPC lightly so LIVE wiring is exercised in dry paths
            try:
                await solana_rpc(
                    self.rpc_url, "getLatestBlockhash",
                    [{"commitment": "processed"}],
                    timeout=self.timeout,
                )
            except Exception as exc:  # noqa: BLE001
                # Dry-run tolerates RPC failure — surface sanitized error only
                log.info("dry buy rpc probe: %s", sanitize_exc(exc))
            tx_id = f"sol_dry_buy_{int(time.time())}"
            return {
                "ok": True,
                "tx_id": tx_id,
                "chain": "solana",
                "token_address": mint,
                "symbol": symbol,
                "stake_size": float(stake_sol),
                "filled": True,
                "dry_run": True,
                "note": "DRY_RUN — set SOLANA_LIVE=1 + SOLANA_WALLET_KEY for live",
            }

        # LIVE path scaffold — wire Jupiter / pump.fun ix builder here
        try:
            await solana_rpc(
                self.rpc_url, "getLatestBlockhash",
                [{"commitment": "confirmed"}],
                timeout=self.timeout,
            )
            # Placeholder: real signing/submit must use solders + never log key
            return {
                "ok": False,
                "error": "live buy not wired — use dry_run or implement pump/Jupiter submit",
                "dry_run": False,
                "token_address": mint,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": sanitize_exc(exc), "dry_run": False}

    async def sell(
        self,
        token_address: str,
        *,
        symbol: str = "",
        fraction: float = 1.0,
        panic: bool = False,
    ) -> dict[str, Any]:
        """
        Market flatten (default 100%). Panic path skips quotes / AI entirely.
        """
        mint = str(token_address or "").strip()
        frac = max(0.0, min(1.0, float(fraction)))
        if not mint:
            return {"ok": False, "error": "missing token_address"}

        if not self.live or not self._has_signer():
            try:
                await solana_rpc(
                    self.rpc_url, "getLatestBlockhash",
                    [{"commitment": "processed"}],
                    timeout=self.timeout,
                )
            except Exception as exc:  # noqa: BLE001
                log.info("dry sell rpc probe: %s", sanitize_exc(exc))
            return {
                "ok": True,
                "tx_id": f"sol_dry_sell_{int(time.time())}",
                "chain": "solana",
                "token_address": mint,
                "symbol": symbol,
                "fraction": frac,
                "filled": True,
                "dry_run": True,
                "panic": bool(panic),
                "note": "DRY_RUN market flatten",
            }

        try:
            await solana_rpc(
                self.rpc_url, "getLatestBlockhash",
                [{"commitment": "confirmed"}],
                timeout=self.timeout,
            )
            return {
                "ok": False,
                "error": "live sell not wired — implement pump/Jupiter sell ix",
                "dry_run": False,
                "panic": bool(panic),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": sanitize_exc(exc), "dry_run": False, "panic": bool(panic)}


# Module-level singleton for trading_loop
_EXEC: SolanaExecutor | None = None


def get_executor() -> SolanaExecutor:
    global _EXEC
    if _EXEC is None:
        _EXEC = SolanaExecutor()
    return _EXEC
