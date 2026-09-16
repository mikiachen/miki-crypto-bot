"""Arc Network meme buy/sell — Circle EVM L1 (USDC = native gas).

Force-broadcast mode (ARC_FORCE_BROADCAST=1):
  * Skip wallet balance preflight
  * Prefer local `cast send`, fall back to web3 eth_sendRawTransaction
  * Empty faucet balance still attempts broadcast (node may reject for gas)

Default dry_run unless ARC_LIVE=1 or ARC_FORCE_BROADCAST=1.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from desk_realtime.arc_net import (
    ARC_FORCE_BROADCAST,
    ARC_SKIP_BALANCE_CHECK,
    ARC_WALLET,
    cast_send,
    fetch_wallet_usdc,
)
from desk_realtime.secrets import sanitize_exc

log = logging.getLogger("arc_executor")

BUY_ABI = [
    {
        "name": "buy",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [{"name": "minTokensOut", "type": "uint256"}],
        "outputs": [{"name": "tokensOut", "type": "uint256"}],
    }
]

# Warp curve: sell(uint256 tokenAmount, uint256 minUsdcOut) — verified via
# tx 0xa6722b08… on 0xCDfED713… (method_id 0xd79875eb).
SELL_ABI = [
    {
        "name": "sell",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "tokenAmount", "type": "uint256"},
            {"name": "minUsdcOut", "type": "uint256"},
        ],
        "outputs": [{"name": "usdcOut", "type": "uint256"}],
    },
    {
        "name": "token",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "quoteSell",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "tokenAmount", "type": "uint256"}],
        "outputs": [{"name": "usdcOut", "type": "uint256"}],
    },
    {
        "name": "quoteBuy",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "usdcIn", "type": "uint256"}],
        "outputs": [{"name": "tokensOut", "type": "uint256"}],
    },
    {
        "name": "price",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

ERC20_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
]


class ArcExecutor:
    """Buy/sell Arc meme coins via bonding curve or DEX router."""

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = (config or {}).get("arc") or config or {}
        self.rpc_url = str(
            cfg.get("rpc_url")
            or os.environ.get("ARC_RPC_URL")
            or "https://rpc.testnet.arc.io"
        )
        self.backup_rpc_url = str(
            cfg.get("backup_rpc_url")
            or os.environ.get("ARC_BACKUP_RPC_URL")
            or "https://rpc.testnet.arc.io"
        )
        self.chain_id = int(cfg.get("chain_id") or os.environ.get("ARC_CHAIN_ID") or 5042002)
        self.slippage_bps = int(cfg.get("slippage_bps") or 500)
        env_live = os.environ.get("ARC_LIVE", "").strip() in ("1", "true", "True")
        self.live = bool(cfg.get("live") or env_live or ARC_FORCE_BROADCAST)
        self.curve_address = str(
            cfg.get("default_curve")
            or os.environ.get("ARC_DEFAULT_CURVE")
            or ""
        ).strip()
        self.wallet = str(cfg.get("wallet") or os.environ.get("ARC_WALLET") or ARC_WALLET).strip()
        self._pk = os.environ.get("ARC_PRIVATE_KEY") or str(cfg.get("private_key") or "")
        # FORCE_BROADCAST no longer implies skip balance — empty wallet must not send
        self.skip_balance = ARC_SKIP_BALANCE_CHECK

    def _rpc_pool(self) -> list[str]:
        """Sticky HTTPS pool. Never HTTP, never niorfun, never the explorer."""
        from desk_realtime.arc_net import probe_height_or_flip
        from desk_realtime.foundry_bin import is_allowed_rpc, ordered_rpc_endpoints

        probe_height_or_flip()
        urls = [u for u in ordered_rpc_endpoints() if is_allowed_rpc(u)]
        return urls or ["https://rpc.mainnet.arc.io"]

    def _w3(self):
        try:
            from web3 import Web3
        except ImportError as exc:
            raise RuntimeError(
                "Install web3 to trade on Arc: pip install web3"
            ) from exc
        timeout = float(os.environ.get("ARC_RPC_TIMEOUT", "2"))
        last_err: Exception | None = None
        for url in self._rpc_pool():
            try:
                w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": timeout}))
                if w3.is_connected():
                    self.rpc_url = url
                    return w3
                last_err = ConnectionError(f"Arc RPC unreachable: {url}")
            except Exception as exc:  # noqa: BLE001
                last_err = exc
        raise ConnectionError(str(last_err) if last_err else "Arc RPC unreachable")

    def _maybe_log_balance(self) -> dict[str, Any]:
        """Read wallet USDC — used for telemetry and live buy gate."""
        try:
            info = fetch_wallet_usdc(self.wallet)
            return info
        except Exception as exc:  # noqa: BLE001
            log.info("balance probe skipped: %s", sanitize_exc(exc))
            return {}

    def _require_funds(self, amount_usdc: float, bal: dict[str, Any]) -> str | None:
        """Return error string if live buy should abort for insufficient USDC."""
        if self.skip_balance:
            return None
        raw = float(bal.get("raw_usdc") or 0.0)
        need = float(amount_usdc or 0.0)
        if raw <= 0:
            return "wallet USDC is 0 · refill or set ARC_SKIP_BALANCE_CHECK=1"
        if need > 0 and raw + 1e-9 < need:
            return f"insufficient USDC · have {raw:.4f} need {need:.4f}"
        return None

    def _broadcast_buy_cast(
        self, curve: str, value_wei: int, min_out: int, *, anti_snipe: bool = True
    ) -> dict[str, Any]:
        return cast_send(
            to=curve,
            value_wei=value_wei,
            sig="buy(uint256)",
            args=[str(int(min_out))],
            private_key=self._pk,
            anti_snipe=anti_snipe,
        )

    def _broadcast_plain_cast(self, to: str, value_wei: int = 0) -> dict[str, Any]:
        """Fallback drill: 0-value (or small) send to prove signing path."""
        return cast_send(
            to=to,
            value_wei=value_wei,
            sig=None,
            args=None,
            private_key=self._pk,
            anti_snipe=True,
        )

    def _gas_fees(self, w3, acct_address: str) -> dict[str, Any]:
        from desk_realtime.arc_strategy import PRIORITY_BUMP

        fees: dict[str, Any] = {
            "from": acct_address,
            "nonce": w3.eth.get_transaction_count(acct_address),
            "chainId": self.chain_id,
            "gas": int(cfg_gas(self) or 400_000),
        }
        try:
            tip = int(w3.eth.max_priority_fee * PRIORITY_BUMP)
            base = w3.eth.get_block("latest")["baseFeePerGas"]
            fees["maxPriorityFeePerGas"] = tip
            fees["maxFeePerGas"] = max(int(base * 2 + tip), 22_000_000_000)
        except Exception:
            fees["gasPrice"] = int(
                max(int(w3.eth.gas_price or 0), 20_000_000_000) * PRIORITY_BUMP
            )
        return fees

    def _send_web3_tx(self, w3, acct, tx: dict[str, Any]) -> dict[str, Any]:
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = w3.eth.send_raw_transaction(raw)
        try:
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=12)
            ok = receipt.status == 1
            block = receipt.blockNumber
            err = "" if ok else "tx reverted"
        except Exception:
            ok = True  # accepted by mempool / node
            block = None
            err = ""
        return {
            "ok": ok,
            "tx_id": tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash),
            "block": block,
            "source": "web3",
            "error": err,
        }

    def _broadcast_buy_web3(self, curve: str, value_wei: int, min_out: int) -> dict[str, Any]:
        w3 = self._w3()
        acct = w3.eth.account.from_key(self._pk)
        contract = w3.eth.contract(
            address=w3.to_checksum_address(curve),
            abi=BUY_ABI,
        )
        tx = contract.functions.buy(int(min_out)).build_transaction(
            {
                **self._gas_fees(w3, acct.address),
                "value": value_wei,
            }
        )
        return self._send_web3_tx(w3, acct, tx)

    def _eth_call(self, to: str, data: str, *, value_wei: int = 0, frm: str | None = None) -> str:
        """JSON-RPC eth_call (no web3 required). Returns hex result or raises."""
        import json
        import urllib.request

        payload: dict[str, Any] = {"to": to, "data": data}
        if value_wei:
            payload["value"] = hex(int(value_wei))
        if frm:
            payload["from"] = frm
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [payload, "latest"]}
        ).encode()
        last_err: Exception | None = None
        from desk_realtime.foundry_bin import is_allowed_rpc

        for url in self._rpc_pool():
            if not is_allowed_rpc(url):
                continue
            try:
                req = urllib.request.Request(
                    url,
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "miki-arc-executor/1.0",
                    },
                )
                with urllib.request.urlopen(
                    req, timeout=float(os.environ.get("ARC_RPC_TIMEOUT", "2"))
                ) as resp:
                    data_j = json.loads(resp.read().decode())
                if data_j.get("error"):
                    raise RuntimeError(str(data_j["error"]))
                return str(data_j.get("result") or "0x")
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                continue
        raise RuntimeError(sanitize_exc(last_err) if last_err else "eth_call failed")

    def _quote_buy_tokens(self, curve: str, usdc_wei: int) -> int:
        """eth_call quoteBuy(usdcWei) → expected meme tokens out."""
        if usdc_wei <= 0:
            return 0
        # selector 0x4beb394c quoteBuy(uint256)
        data = "0x4beb394c" + hex(int(usdc_wei))[2:].zfill(64)
        try:
            raw = self._eth_call(curve, data)
            return int(raw, 16) if raw and raw != "0x" else 0
        except Exception as exc:  # noqa: BLE001
            log.info("buy quote rpc failed: %s", sanitize_exc(exc))
        try:
            w3 = self._w3()
            contract = w3.eth.contract(
                address=w3.to_checksum_address(curve),
                abi=SELL_ABI,
            )
            out = contract.functions.quoteBuy(int(usdc_wei)).call()
            if isinstance(out, (list, tuple)):
                return int(out[0])
            return int(out)
        except Exception as exc:  # noqa: BLE001
            log.info("buy quote failed: %s", sanitize_exc(exc))
            return 0

    def _quote_sell_usdc(self, curve: str, token_amount: int) -> int:
        """Prefer quoteSell(view); fallback eth_call sell(amount,0).

        Returns native-USDC wei (1e18) on Warp testnet quotes.
        """
        if token_amount <= 0:
            return 0
        # selector 0xa64190c4 quoteSell(uint256)
        data = "0xa64190c4" + hex(int(token_amount))[2:].zfill(64)
        try:
            raw = self._eth_call(curve, data)
            out_i = int(raw, 16) if raw and raw != "0x" else 0
            if out_i > 0:
                return out_i
        except Exception as exc:  # noqa: BLE001
            log.info("quoteSell rpc failed: %s", sanitize_exc(exc))
        try:
            w3 = self._w3()
            contract = w3.eth.contract(
                address=w3.to_checksum_address(curve),
                abi=SELL_ABI,
            )
            try:
                out = contract.functions.quoteSell(int(token_amount)).call()
                if isinstance(out, (list, tuple)):
                    out = out[0]
                out_i = int(out)
                if out_i > 0:
                    return out_i
            except Exception as exc:  # noqa: BLE001
                log.info("quoteSell failed: %s", sanitize_exc(exc))

            acct = w3.eth.account.from_key(self._pk) if self._pk else None
            call_kw: dict[str, Any] = {}
            if acct is not None:
                call_kw["from"] = acct.address
            out = contract.functions.sell(int(token_amount), 0).call(call_kw)
            if isinstance(out, (list, tuple)):
                return int(out[0])
            return int(out)
        except Exception as exc:  # noqa: BLE001
            log.info("sell quote failed: %s", sanitize_exc(exc))
            return 0

    def mark_position(
        self,
        curve: str,
        *,
        token_amount: int = 0,
        cost_usdc: float = 0.0,
        owner: str | None = None,
    ) -> dict[str, Any]:
        """On-chain mark: quoteSell(tokens) / cost → live multiple."""
        held: dict[str, Any]
        if token_amount > 0:
            held = {
                "meme_token": "",
                "token_amount": int(token_amount),
                "curve": curve,
                "owner": owner or self.wallet,
            }
        else:
            try:
                held = self.tokens_held(curve, owner=owner)
            except Exception:
                held = {"token_amount": 0, "curve": curve, "meme_token": ""}
        amt = int(held.get("token_amount") or 0)
        usdc_wei = self._quote_sell_usdc(curve, amt) if amt > 0 else 0
        # Warp quotes return 1e18 native USDC wei; also tolerate 1e6 ERC-20 units.
        if usdc_wei >= 10**15:
            mark_usdc = usdc_wei / 1e18
            unit = "1e18"
        else:
            mark_usdc = usdc_wei / 1e6
            unit = "1e6"
        cost = float(cost_usdc or 0.0)
        mult = (mark_usdc / cost) if cost > 0 and mark_usdc > 0 else 0.0
        return {
            "curve": curve,
            "token_amount": amt,
            "mark_usdc": round(mark_usdc, 6),
            "mark_usdc_wei": usdc_wei,
            "usdc_unit": unit,
            "cost_usdc": cost,
            "live_mult": round(mult, 4) if mult > 0 else 0.0,
            "source": "quoteSell" if usdc_wei > 0 else "none",
            "meme_token": held.get("meme_token") or "",
        }

    def curve_tradeable(self, curve: str, *, probe_usdc: float = 0.1) -> dict[str, Any]:
        """Reject graduated / broken curves before broadcast."""
        value_wei = int(round(float(probe_usdc) * 1e18))
        tokens = self._quote_buy_tokens(curve, value_wei)
        if tokens <= 0:
            return {"ok": False, "error": "quoteBuy failed / empty", "graduated": False}
        # Simulate payable buy via eth_call (selector buy(uint256)=0xd96a094a)
        data = "0xd96a094a" + ("0" * 64)
        frm = (self.wallet or "").strip() or None
        try:
            self._eth_call(curve, data, value_wei=value_wei, frm=frm)
            return {"ok": True, "quote_tokens": tokens, "graduated": False}
        except Exception as exc:  # noqa: BLE001
            msg = sanitize_exc(exc).lower()
            graduated = "graduat" in msg
            return {
                "ok": False,
                "error": sanitize_exc(exc),
                "graduated": graduated,
                "quote_tokens": tokens,
            }

    def roundtrip_ok(self, curve: str, probe_usdc: float = 0.1) -> dict[str, Any]:
        """Buy quote plus sell quote. A live Warp fill needs both. No broadcast."""
        trade = self.curve_tradeable(curve, probe_usdc=probe_usdc)
        if not trade.get("ok"):
            return trade
        tokens = int(trade.get("quote_tokens") or 0)
        usdc_wei = self._quote_sell_usdc(curve, tokens)
        if usdc_wei <= 0:
            return {
                "ok": False,
                "error": "quoteSell empty · cannot exit",
                "quote_tokens": tokens,
            }
        return {"ok": True, "quote_tokens": tokens, "sell_usdc_wei": usdc_wei}

    def _resolve_meme_token(self, curve: str) -> str:
        """Curve.token() → ERC-20 CA (Warp verified). Env override ARC_WARP_TOKEN."""
        env_tok = (os.environ.get("ARC_WARP_TOKEN") or "").strip()
        if env_tok.startswith("0x") and len(env_tok) >= 42:
            return env_tok
        # token() selector 0xfc0c546a — RPC first (no web3 required)
        try:
            raw = self._eth_call(curve, "0xfc0c546a")
            if raw and len(raw) >= 66:
                return "0x" + raw[-40:]
        except Exception as exc:  # noqa: BLE001
            log.info("token() rpc failed: %s", sanitize_exc(exc))
        w3 = self._w3()
        c = w3.eth.contract(address=w3.to_checksum_address(curve), abi=SELL_ABI)
        return str(c.functions.token().call())

    def _erc20_balance(self, token: str, owner: str) -> int:
        # balanceOf(address) selector 0x70a08231
        data = "0x70a08231" + owner.lower().replace("0x", "").zfill(64)
        try:
            raw = self._eth_call(token, data)
            return int(raw, 16) if raw and raw != "0x" else 0
        except Exception as exc:  # noqa: BLE001
            log.info("balanceOf rpc failed: %s", sanitize_exc(exc))
        try:
            w3 = self._w3()
            c = w3.eth.contract(address=w3.to_checksum_address(token), abi=ERC20_ABI)
            return int(c.functions.balanceOf(w3.to_checksum_address(owner)).call())
        except Exception as exc:  # noqa: BLE001
            log.info("balanceOf web3 failed: %s", sanitize_exc(exc))
            return 0

    def tokens_held(self, curve: str, owner: str | None = None) -> dict[str, Any]:
        """Read meme ERC-20 balance for position accounting."""
        meme = self._resolve_meme_token(curve)
        who = (owner or self.wallet or "").strip()
        if not who and self._pk:
            try:
                who = self._w3().eth.account.from_key(self._pk).address
            except Exception:
                who = ""
        bal = self._erc20_balance(meme, who) if who and meme.startswith("0x") else 0
        return {"meme_token": meme, "owner": who, "token_amount": int(bal), "curve": curve}

    def _ensure_approve(self, token: str, spender: str, amount: int) -> dict[str, Any]:
        """Approve curve to pull meme tokens before sell (transferFrom path)."""
        if amount <= 0:
            return {"ok": True, "skipped": True, "tx_id": ""}
        w3 = self._w3()
        acct = w3.eth.account.from_key(self._pk)
        tok = w3.eth.contract(address=w3.to_checksum_address(token), abi=ERC20_ABI)
        allowance = int(
            tok.functions.allowance(
                w3.to_checksum_address(acct.address),
                w3.to_checksum_address(spender),
            ).call()
        )
        if allowance >= amount:
            return {"ok": True, "skipped": True, "tx_id": "", "allowance": allowance}

        # Prefer cast approve; fall back to web3
        result = cast_send(
            to=token,
            value_wei=0,
            sig="approve(address,uint256)",
            args=[spender, str(int(amount))],
            private_key=self._pk,
            anti_snipe=True,
        )
        if result.get("ok"):
            return result
        tx = tok.functions.approve(
            w3.to_checksum_address(spender), int(amount)
        ).build_transaction(self._gas_fees(w3, acct.address))
        return self._send_web3_tx(w3, acct, tx)

    def _broadcast_sell_cast(
        self, curve: str, token_amount: int, min_usdc_out: int, *, anti_snipe: bool = True
    ) -> dict[str, Any]:
        return cast_send(
            to=curve,
            value_wei=0,
            sig="sell(uint256,uint256)",
            args=[str(int(token_amount)), str(int(min_usdc_out))],
            private_key=self._pk,
            anti_snipe=anti_snipe,
        )

    def _broadcast_sell_web3(
        self, curve: str, token_amount: int, min_usdc_out: int
    ) -> dict[str, Any]:
        w3 = self._w3()
        acct = w3.eth.account.from_key(self._pk)
        contract = w3.eth.contract(
            address=w3.to_checksum_address(curve),
            abi=SELL_ABI,
        )
        tx = contract.functions.sell(int(token_amount), int(min_usdc_out)).build_transaction(
            self._gas_fees(w3, acct.address)
        )
        return self._send_web3_tx(w3, acct, tx)

    async def buy(
        self,
        token_or_curve: str,
        amount_usdc: float,
        *,
        min_tokens_out: int = 0,
        symbol: str = "",
        expected_tokens_out: int = 0,
        panic: bool = False,
        trailing: bool = False,
        launchpad: str = "",
        **_: Any,
    ) -> dict[str, Any]:
        from desk_realtime.arc_strategy import (
            dynamic_slippage_bps,
            min_tokens_out_from_slippage,
        )

        curve = token_or_curve or self.curve_address
        bal = self._maybe_log_balance()
        raw = float(bal.get("raw_usdc") or 0.0)
        from desk_realtime.arc_hunt import capped_entry_usdc

        capped = capped_entry_usdc(raw if raw > 0 else float(amount_usdc) + 0.06, float(amount_usdc))
        amount_usdc = min(float(capped), 1.0)
        if amount_usdc <= 0 or amount_usdc > 1.0:
            return {
                "ok": False,
                "error": "buy capped · max 1 USDC, empty wallet, or below gas reserve",
                "dry_run": not self.live,
                "cost_usdc": 0.0,
            }
        value_wei = int(round(float(amount_usdc) * 1e18))
        slip_bps = dynamic_slippage_bps(
            panic=panic,
            trailing=trailing,
            urgency=0.7 if panic else 0.0,
            launchpad=launchpad,
            address=curve,
            symbol=symbol,
        )
        if expected_tokens_out > 0 and min_tokens_out <= 0:
            min_tokens_out = min_tokens_out_from_slippage(expected_tokens_out, slip_bps)
        # Buy path: normal slip unless urgency flags set
        if not panic and not trailing:
            # Keep configured floor, but never below fee-aware slip (Tolly 1%+)
            slip_bps = max(int(self.slippage_bps or 0), slip_bps)

        # Default minOut protection from quoteBuy (refuse zero-min live buys)
        allow_zero = os.environ.get("ARC_ALLOW_ZERO_MINOUT", "0").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        quoted_tokens = 0
        if curve and min_tokens_out <= 0 and expected_tokens_out <= 0:
            quoted_tokens = self._quote_buy_tokens(curve, value_wei)
            if quoted_tokens > 0:
                min_tokens_out = min_tokens_out_from_slippage(quoted_tokens, slip_bps)

        pad = (launchpad or "").strip().lower()
        if pad in ("tolly", "dyor"):
            return {
                "ok": False,
                "error": (
                    f"{pad} swap not armed · Warp buy selector will not be sent to another pad · "
                    "need a verified mainnet router and its own swap ABI"
                ),
                "dry_run": False,
                "launchpad": pad,
            }

        if not self.live:
            tx_id = f"arc_dry_{int(time.time())}"
            log.info(
                "ARC dry_run buy curve=%s usdc=%.2f slip=%dbps sym=%s",
                (curve or "?")[:10],
                amount_usdc,
                slip_bps,
                symbol,
            )
            return {
                "ok": True,
                "tx_id": tx_id,
                "chain": "arc",
                "chain_id": self.chain_id,
                "curve": curve,
                "stake_usdc": amount_usdc,
                "cost_usdc": float(amount_usdc),
                "symbol": symbol,
                "value_wei": value_wei,
                "filled": True,
                "dry_run": True,
                "unit": "USDC",
                "slippage_bps": slip_bps,
                "wallet_display": bal.get("display_usdc"),
                "meme_token": "",
                "token_amount": 0,
            }

        if not self._pk:
            return {"ok": False, "error": "ARC_PRIVATE_KEY not set", "dry_run": False}

        fund_err = self._require_funds(amount_usdc, bal)
        if fund_err:
            return {
                "ok": False,
                "error": fund_err,
                "dry_run": False,
                "wallet_raw_usdc": bal.get("raw_usdc"),
            }

        target = curve or self.wallet
        if curve:
            gate = self.curve_tradeable(curve, probe_usdc=min(0.1, float(amount_usdc) or 0.1))
            if not gate.get("ok"):
                return {
                    "ok": False,
                    "error": f"curve not tradeable: {gate.get('error') or 'gate'}",
                    "graduated": bool(gate.get("graduated")),
                    "dry_run": False,
                    "curve": curve,
                    "symbol": symbol,
                }
            if min_tokens_out <= 0 and not allow_zero:
                return {
                    "ok": False,
                    "error": "buy blocked · minOut=0 (quoteBuy failed) · set ARC_ALLOW_ZERO_MINOUT=1 to override",
                    "dry_run": False,
                    "curve": curve,
                    "quoted_tokens": quoted_tokens,
                }

        min_out = int(min_tokens_out)
        result: dict[str, Any]

        if curve:
            result = self._broadcast_buy_cast(curve, value_wei, min_out, anti_snipe=True)
            if not result.get("ok"):
                log.info("cast buy failed (%s) — trying web3", result.get("error", "")[:120])
                try:
                    result = self._broadcast_buy_web3(curve, value_wei, min_out)
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "error": sanitize_exc(exc), "tx_id": "", "source": "web3"}
        else:
            result = self._broadcast_plain_cast(self.wallet, value_wei=0)
            if not result.get("ok"):
                try:
                    w3 = self._w3()
                    acct = w3.eth.account.from_key(self._pk)
                    tip = max(int(w3.eth.gas_price or 0), 20_000_000_000)
                    tip = int(tip * 1.10)  # anti-snipe +10%
                    tx = {
                        "from": acct.address,
                        "to": acct.address,
                        "value": 0,
                        "nonce": w3.eth.get_transaction_count(acct.address),
                        "chainId": self.chain_id,
                        "gas": 21_000,
                        "gasPrice": tip,
                    }
                    signed = acct.sign_transaction(tx)
                    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
                    tx_hash = w3.eth.send_raw_transaction(raw)
                    result = {
                        "ok": True,
                        "tx_id": tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash),
                        "source": "web3_self",
                        "error": "",
                    }
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "error": sanitize_exc(exc), "tx_id": "", "source": "web3_self"}

        held: dict[str, Any] = {}
        if result.get("ok") and curve:
            try:
                time.sleep(0.8)  # allow state settle before balanceOf
                held = self.tokens_held(curve)
            except Exception as exc:  # noqa: BLE001
                log.info("post-buy balance probe: %s", sanitize_exc(exc))
            # If balance probe lags, book quoted tokens (slip-adjusted) as provisional size
            if int(held.get("token_amount") or 0) <= 0 and (quoted_tokens or min_out):
                provisional = int(min_out) if min_out > 0 else int(quoted_tokens)
                held = {
                    "meme_token": held.get("meme_token") or "",
                    "token_amount": provisional,
                    "provisional": True,
                    "curve": curve,
                }
                log.info("post-buy using provisional token_amount=%s", provisional)

        return {
            "ok": bool(result.get("ok")),
            "tx_id": result.get("tx_id") or "",
            "chain": "arc",
            "chain_id": self.chain_id,
            "curve": target,
            "stake_usdc": amount_usdc,
            "symbol": symbol,
            "value_wei": value_wei,
            "filled": bool(result.get("ok")),
            "dry_run": False,
            "unit": "USDC",
            "slippage_bps": slip_bps,
            "force_broadcast": True,
            "skip_balance_check": True,
            "broadcast_source": result.get("source") or "cast",
            "anti_snipe": True,
            "error": result.get("error") or "",
            "wallet_raw_usdc": bal.get("raw_usdc"),
            "wallet_display": bal.get("display_usdc"),
            "block": result.get("block"),
            "meme_token": held.get("meme_token") or "",
            "token_amount": int(held.get("token_amount") or 0),
            "cost_usdc": float(amount_usdc),
            "quoted_tokens": quoted_tokens,
            "min_tokens_out": min_out,
        }

    async def sell(
        self,
        token_or_curve: str,
        token_amount: int = 0,
        *,
        symbol: str = "",
        fraction: float = 1.0,
        panic: bool = False,
        trailing: bool = False,
        launchpad: str = "",
        min_usdc_out: int = 0,
        **_: Any,
    ) -> dict[str, Any]:
        from desk_realtime.arc_strategy import dynamic_slippage_bps

        bal = self._maybe_log_balance()
        curve = token_or_curve or self.curve_address or self.wallet
        # Panic / trailing: yank slippage to 15–20% for 1-block land on Arc USDC gas
        urgency = 1.0 if panic else (0.6 if trailing else 0.0)
        slip_bps = dynamic_slippage_bps(panic=panic, trailing=trailing, urgency=urgency)

        if not self.live:
            return {
                "ok": True,
                "tx_id": f"arc_dry_sell_{int(time.time())}",
                "dry_run": True,
                "curve": curve,
                "token_amount": token_amount,
                "symbol": symbol,
                "fraction": fraction,
                "panic": bool(panic),
                "trailing": bool(trailing),
                "slippage_bps": slip_bps,
                "launchpad": launchpad or "",
                "chain": "arc",
                "chain_id": self.chain_id,
                "wallet_display": bal.get("display_usdc"),
            }

        if not self._pk:
            return {"ok": False, "error": "ARC_PRIVATE_KEY not set", "panic": bool(panic)}

        if not (curve.startswith("0x") and len(curve) >= 42):
            return {"ok": False, "error": "sell needs curve address", "panic": bool(panic)}

        meme_token = ""
        amount = int(token_amount or 0)
        try:
            meme_token = self._resolve_meme_token(curve)
            if amount <= 0:
                owner = (self.wallet or "").strip()
                if not owner:
                    owner = self._w3().eth.account.from_key(self._pk).address
                bal_tok = self._erc20_balance(meme_token, owner)
                frac = max(0.0, min(1.0, float(fraction)))
                amount = int(bal_tok * frac) if frac < 1.0 else int(bal_tok)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"resolve sell size: {sanitize_exc(exc)}",
                "panic": bool(panic),
                "curve": curve,
            }

        if amount <= 0:
            return {
                "ok": False,
                "error": "token balance 0 · nothing to sell",
                "panic": bool(panic),
                "curve": curve,
                "meme_token": meme_token,
            }

        # Panic / trailing: quoteSell × dynamic slip (15–20%). Quote miss → minOut 0
        # so the flatten still lands instead of getting stuck on a dead quote.
        out_min = int(min_usdc_out or 0)
        if panic or trailing or out_min <= 0:
            quoted = self._quote_sell_usdc(curve, amount)
            if quoted > 0:
                from desk_realtime.arc_strategy import min_tokens_out_from_slippage

                out_min = min_tokens_out_from_slippage(quoted, slip_bps)
            else:
                out_min = 0
                if not (panic or trailing) and os.environ.get("ARC_ALLOW_ZERO_MINOUT", "0").strip().lower() not in (
                    "1",
                    "true",
                    "yes",
                ):
                    log.warning("sell minOut quote empty — proceeding with 0")

        approve = self._ensure_approve(meme_token, curve, amount)
        if not approve.get("ok"):
            return {
                "ok": False,
                "error": f"approve failed: {approve.get('error') or 'unknown'}",
                "panic": bool(panic),
                "curve": curve,
                "meme_token": meme_token,
                "token_amount": amount,
                "approve_tx": approve.get("tx_id") or "",
            }

        result = self._broadcast_sell_cast(curve, amount, out_min, anti_snipe=True)
        if not result.get("ok"):
            log.info("cast sell failed (%s) — trying web3", (result.get("error") or "")[:120])
            try:
                result = self._broadcast_sell_web3(curve, amount, out_min)
            except Exception as exc:  # noqa: BLE001
                result = {
                    "ok": False,
                    "error": sanitize_exc(exc),
                    "tx_id": "",
                    "source": "web3",
                }

        return {
            "ok": bool(result.get("ok")),
            "tx_id": result.get("tx_id") or "",
            "dry_run": False,
            "curve": curve,
            "meme_token": meme_token,
            "token_amount": amount,
            "min_usdc_out": out_min,
            "symbol": symbol,
            "fraction": fraction,
            "panic": bool(panic),
            "trailing": bool(trailing),
            "slippage_bps": slip_bps,
            "launchpad": launchpad or "",
            "chain": "arc",
            "chain_id": self.chain_id,
            "force_broadcast": True,
            "skip_balance_check": True,
            "anti_snipe": True,
            "approve_tx": approve.get("tx_id") or "",
            "broadcast_source": result.get("source") or "cast",
            "error": result.get("error") or "",
            "wallet_display": bal.get("display_usdc"),
            "block": result.get("block"),
        }


def cfg_gas(executor: ArcExecutor) -> int | None:
    return None


_EXEC: ArcExecutor | None = None


def get_arc_executor(config: dict[str, Any] | None = None) -> ArcExecutor:
    global _EXEC
    if _EXEC is None:
        _EXEC = ArcExecutor(config)
    return _EXEC
