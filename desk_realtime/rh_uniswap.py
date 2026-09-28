"""Uniswap v3 on Robinhood Chain (4663). Quote via QuoterV2; optional Trading API local sign.

Addresses from Uniswap v3 Robinhood Chain deployments.
Broadcast stays off until RH_UNI_SEND=1. Hard cap via RH_BET_HARD_CAP (quote currency USDG).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

from desk_realtime.rh_net import (
    USDG,
    WETH,
    broadcast_raw,
    chain_id,
    eth_call,
    private_key,
    rpc_endpoints,
    rpc_json,
    wait_receipt,
    wallet,
)
from desk_realtime.secrets import sanitize_exc

log = logging.getLogger("rh_uniswap")

QUOTER = "0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7"
SWAP_ROUTER = "0xcaf681a66d020601342297493863e78c959e5cb2"
UNIVERSAL_ROUTER = "0x8876789976decbfcbbbe364623c63652db8c0904"
FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
_ALLOWANCE = "0xdd62ed3e"
_APPROVE = "0x095ea7b3"
_DECIMALS = "0x313ce567"
_BALANCE = "0x70a08231"
_QUOTE_V2 = "0xc6a5026a"
_FEES = (10000, 3000, 500, 100)
_TRADE_API = "https://trade-api.gateway.uniswap.org/v1"
_HARD_CAP = 2.0  # USDG ceiling for RH_BET_HARD_CAP


def send_armed() -> bool:
    return os.environ.get("RH_UNI_SEND", "0").strip().lower() in ("1", "true", "yes", "on")


def _api_key() -> str:
    return (os.environ.get("UNISWAP_API_KEY") or "").strip()


def hard_cap() -> float:
    try:
        return max(0.01, min(_HARD_CAP, float(os.environ.get("RH_BET_HARD_CAP", "1.5"))))
    except ValueError:
        return 1.5


def min_roundtrip() -> float:
    """Minimum fraction of USDG a buy-then-sell quote must return. Below this, no buy."""
    try:
        return min(0.999, max(0.5, float(os.environ.get("RH_MIN_ROUNDTRIP", "0.97"))))
    except ValueError:
        return 0.97


def _pad_addr(addr: str) -> str:
    return addr.lower().replace("0x", "").zfill(64)


def _pad_int(n: int) -> str:
    return format(int(n), "064x")


def _eth_call(to: str, data: str) -> str:
    try:
        return eth_call(to, data)
    except Exception as exc:  # noqa: BLE001
        log.info("rh eth_call: %s", sanitize_exc(exc))
        return ""


def erc20_decimals(token: str) -> int:
    raw = _eth_call(token, _DECIMALS)
    if len(raw) < 66:
        return 0
    return int(raw, 16)


def erc20_balance(token: str, owner: str = "", *, timeout: float = 8.0) -> int:
    ok, amt = erc20_balance_read(token, owner, timeout=timeout)
    return amt if ok else 0


def erc20_balance_read(
    token: str, owner: str = "", *, timeout: float = 8.0
) -> tuple[bool, int]:
    """(True, amount) on a real read. (False, 0) means the RPC did not answer."""
    who = (owner or wallet()).strip()
    if not (token.startswith("0x") and len(token) == 42 and who.startswith("0x") and len(who) == 42):
        return False, 0
    try:
        # UI passes short timeout; trading keeps default 8s.
        raw = eth_call(
            token,
            _BALANCE + _pad_addr(who),
            timeout=float(timeout),
            max_endpoints=1 if float(timeout) <= 2.0 else 2,
        )
    except Exception as exc:  # noqa: BLE001
        log.info("balance unread: %s", sanitize_exc(exc))
        return False, 0
    if not isinstance(raw, str) or len(raw) < 66:
        return False, 0
    return True, int(raw, 16)


def _quote_call(token_in: str, token_out: str, amount: int, fee: int) -> int:
    data = (
        _QUOTE_V2
        + _pad_addr(token_in)
        + _pad_addr(token_out)
        + _pad_int(amount)
        + _pad_int(fee)
        + _pad_int(0)
    )
    raw = _eth_call(QUOTER, data)
    if len(raw) < 66:
        return 0
    return int(raw[2:66], 16)


def _best_direct(token_in: str, token_out: str, amount: int) -> tuple[int, int]:
    """Return (fee, amount_out) for best single-hop fee tier."""
    best = (0, 0)
    for fee in _FEES:
        try:
            out = _quote_call(token_in, token_out, amount, fee)
        except Exception:
            out = 0
        if out > best[1]:
            best = (fee, out)
    return best


def _quote_buy_usdg(token: str, amount_usdg: int) -> tuple[int, int, str]:
    """USDG → token. Direct first, else USDG → WETH → token. Returns (tokens, fee_tag, route)."""
    fee, bought = _best_direct(USDG, token, amount_usdg)
    if bought > 0:
        return bought, fee, "direct"
    fee1, weth_out = _best_direct(USDG, WETH, amount_usdg)
    if weth_out <= 0:
        return 0, 0, ""
    fee2, bought = _best_direct(WETH, token, weth_out)
    if bought <= 0:
        return 0, 0, ""
    return bought, fee2, f"viaWETH/{fee1}"


def _quote_sell_usdg(token: str, amount_token: int) -> tuple[int, int, str]:
    """token → USDG. Direct first, else token → WETH → USDG."""
    fee, sold = _best_direct(token, USDG, amount_token)
    if sold > 0:
        return sold, fee, "direct"
    fee1, weth_out = _best_direct(token, WETH, amount_token)
    if weth_out <= 0:
        return 0, 0, ""
    fee2, sold = _best_direct(WETH, USDG, weth_out)
    if sold <= 0:
        return 0, 0, ""
    return sold, fee2, f"viaWETH/{fee1}"


def _amount_out(payload: dict[str, Any]) -> int:
    """Raw output amount from a Trading API /quote body. 0 if unread."""
    if not isinstance(payload, dict):
        return 0
    quote = payload.get("quote") if isinstance(payload.get("quote"), dict) else payload
    for node in (quote, payload):
        if not isinstance(node, dict):
            continue
        out = node.get("output") if isinstance(node.get("output"), dict) else None
        if out is None and isinstance(node.get("amountOut"), dict):
            out = node.get("amountOut")
        if isinstance(out, dict):
            raw = out.get("amount") or out.get("minimumAmount") or ""
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
        raw = node.get("amountOut")
        if isinstance(raw, str) and raw.isdigit():
            return int(raw)
    return 0


def _route_label(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, default=str).lower()
    routing = ""
    if isinstance(payload, dict):
        routing = str(payload.get("routing") or "")
        quote = payload.get("quote") if isinstance(payload.get("quote"), dict) else {}
        routing = routing or str(quote.get("routing") or "")
    ver = "v4" if "v4" in blob else ("v3" if "v3" in blob else "api")
    return f"{routing or 'BEST_PRICE'}/{ver}"


def _api_quote(token_in: str, token_out: str, amount: int) -> dict[str, Any]:
    return _trade_post(
        "/quote",
        {
            "tokenIn": token_in,
            "tokenOut": token_out,
            "tokenInChainId": chain_id(),
            "tokenOutChainId": chain_id(),
            "amount": str(int(amount)),
            "type": "EXACT_INPUT",
            "swapper": wallet(),
            "slippageTolerance": float(os.environ.get("RH_SLIPPAGE_PCT", "1.0")),
            "routingPreference": "BEST_PRICE",
        },
    )


def quote_roundtrip(token: str, *, usdg_in: float | None = None) -> dict[str, Any]:
    """Buy then sell via Uniswap Trading API BEST_PRICE (v3 and v4). No broadcast.

    A quote that returns clearly less than the USDG in is not tradable.
    """
    token = (token or "").strip()
    size = float(usdg_in if usdg_in is not None else hard_cap())
    base: dict[str, Any] = {
        "ok": False,
        "token": token,
        "usdg_in": size,
        "usdg_out": 0.0,
        "fee": 0,
        "send": False,
        "reason": "quote unread",
        "chain_id": chain_id(),
    }
    if not (token.startswith("0x") and len(token) == 42):
        base["reason"] = "token unread"
        return base
    if token.lower() == USDG.lower():
        base["reason"] = "quote token is USDG"
        return base
    try:
        decimals = erc20_decimals(USDG)
    except Exception as exc:  # noqa: BLE001
        base["reason"] = f"usdg decimals unread · {sanitize_exc(exc)}"
        return base
    if decimals != 6:
        base["reason"] = f"usdg decimals {decimals} · expected 6"
        return base
    amount_in = int(round(size * 1_000_000))
    cap = int(round(hard_cap() * 1_000_000))
    if amount_in <= 0 or amount_in > max(cap, 1_000_000):
        base["reason"] = "quote size outside hard cap"
        return base
    if not _api_key():
        base["reason"] = "UNISWAP_API_KEY unread"
        return base
    try:
        buy = _api_quote(USDG, token, amount_in)
    except Exception as exc:  # noqa: BLE001
        base["reason"] = f"api quote failed · {sanitize_exc(exc)}"
        return base
    bought = _amount_out(buy)
    if bought <= 0:
        base["reason"] = "api buy quote empty · no send"
        return base
    try:
        sell = _api_quote(token, USDG, bought)
    except Exception as exc:  # noqa: BLE001
        base["reason"] = f"api sell quote failed · {sanitize_exc(exc)}"
        return base
    sold = _amount_out(sell)
    if sold <= 0:
        base["reason"] = "api sell quote empty · no send"
        return base
    usdg_out = sold / 1_000_000
    floor = float(size) * min_roundtrip()
    route = _route_label(buy)
    ok = usdg_out + 1e-9 >= floor
    reason = (
        f"quote {amount_in / 1_000_000:.4f}→{usdg_out:.4f} · {route}"
        if ok
        else f"roundtrip {usdg_out:.4f} < {floor:.4f} · {route}"
    )
    return {
        "ok": ok,
        "token": token,
        "usdg_in": round(amount_in / 1_000_000, 6),
        "usdg_out": round(usdg_out, 6),
        "tokens_out": bought,
        "fee": 0,
        "route": route,
        "sell_route": _route_label(sell),
        "send": False,
        "reason": reason,
        "chain_id": chain_id(),
    }


def quote_held_mark(token: str, *, token_amount: int = 0, cost_usdg: float = 0.0) -> dict[str, Any]:
    token = (token or "").strip()
    amt = int(token_amount or 0)
    if amt <= 0 and token.startswith("0x"):
        amt = erc20_balance(token)
    empty = {
        "token": token,
        "token_amount": amt,
        "mark_usdg": 0.0,
        "live_mult": 0.0,
        "source": "none",
    }
    if amt <= 0:
        return empty
    try:
        sell = _api_quote(token, USDG, amt)
        sold = _amount_out(sell)
        route = _route_label(sell)
    except Exception:
        sold, route = 0, ""
    if sold <= 0:
        sold, _, route = _quote_sell_usdg(token, amt)
    if sold <= 0:
        return empty
    mark = sold / 1_000_000
    cost = float(cost_usdg or 0.0)
    mult = (mark / cost) if cost > 0 else 0.0
    return {
        "token": token,
        "token_amount": amt,
        "mark_usdg": round(mark, 6),
        "live_mult": round(mult, 4),
        "source": f"uniQuote/{route}",
    }


def _trade_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    key = _api_key()
    if not key:
        raise RuntimeError("UNISWAP_API_KEY not set")
    raw = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{_TRADE_API}{path}",
        data=raw,
        headers={
            "x-api-key": key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-universal-router-version": "2.1.1",
            "User-Agent": "miki-rh-uni/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        detail = ""
        if hasattr(exc, "read"):
            try:
                detail = exc.read().decode(errors="replace")[:240]
            except Exception:
                detail = ""
        raise RuntimeError(sanitize_exc(detail or exc)) from None
    if not isinstance(data, dict):
        raise RuntimeError("trade api shape unexpected")
    return data


def _hex_int(raw: Any) -> int:
    if raw is None or raw == "":
        return 0
    if isinstance(raw, int):
        return int(raw)
    text = str(raw).strip()
    if text.startswith("0x"):
        return int(text, 16)
    return int(float(text))


def _sign_and_send(tx: dict[str, Any]) -> dict[str, Any]:
    from eth_account import Account
    from web3 import Web3

    pk = private_key()
    if not pk:
        return {"ok": False, "error": "RH_PRIVATE_KEY not set", "tx_id": ""}
    if not send_armed():
        return {"ok": False, "error": "RH_UNI_SEND off · no broadcast", "tx_id": ""}
    to = str(tx.get("to") or "")
    data = str(tx.get("data") or "")
    value = _hex_int(tx.get("value"))
    rpc = rpc_endpoints()[0]
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 25}))
    acct = Account.from_key(pk)
    built: dict[str, Any] = {
        "to": Web3.to_checksum_address(to),
        "from": acct.address,
        "data": data,
        "value": value,
        "nonce": w3.eth.get_transaction_count(acct.address, "pending"),
        "chainId": chain_id(),
        "gas": _hex_int(tx.get("gasLimit") or tx.get("gas") or 450_000) or 450_000,
    }
    if tx.get("maxFeePerGas") and tx.get("maxPriorityFeePerGas"):
        built["maxFeePerGas"] = _hex_int(tx.get("maxFeePerGas"))
        built["maxPriorityFeePerGas"] = _hex_int(tx.get("maxPriorityFeePerGas"))
    elif tx.get("gasPrice"):
        built["gasPrice"] = _hex_int(tx.get("gasPrice"))
    else:
        tip = max(int(getattr(w3.eth, "max_priority_fee", 0) or 0), 50_000_000)
        base_fee = int((w3.eth.get_block("latest") or {}).get("baseFeePerGas") or tip)
        built["maxPriorityFeePerGas"] = tip
        built["maxFeePerGas"] = max(base_fee * 2 + tip, tip * 2)
    signed = acct.sign_transaction(built)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    hex_raw = raw.hex() if hasattr(raw, "hex") else str(raw)
    if not hex_raw.startswith("0x"):
        hex_raw = f"0x{hex_raw}"
    return broadcast_raw(hex_raw)


def _sign_permit(permit: dict[str, Any]) -> str:
    """EIP-712 Permit2 signature. Off-chain. Never reuse across quotes."""
    from eth_account import Account

    domain = dict(permit.get("domain") or {})
    types = {
        key: val
        for key, val in dict(permit.get("types") or {}).items()
        if key != "EIP712Domain"
    }
    values = permit.get("values") or permit.get("message") or {}
    if "chainId" in domain:
        domain["chainId"] = int(domain["chainId"])
    signed = Account.sign_typed_data(
        private_key(),
        domain_data=domain,
        message_types=types,
        message_data=values,
    )
    sig = signed.signature.hex()
    return sig if sig.startswith("0x") else f"0x{sig}"


def _tx_step(step: Any) -> dict[str, Any] | None:
    if not isinstance(step, dict):
        return None
    if not step.get("to") or not step.get("data"):
        return None
    return step


def ensure_token_approval(token: str, amount: str, *, token_out: str = "") -> dict[str, Any]:
    """Permit2 must be able to pull the token. Chain allowance wins over the API."""
    who = wallet()
    if not who:
        return {"ok": False, "error": "RH_WALLET unread", "tx_id": ""}
    body: dict[str, Any] = {
        "walletAddress": who,
        "token": token,
        "amount": str(amount),
        "chainId": chain_id(),
        "includeGasInfo": True,
    }
    if token_out.startswith("0x"):
        body["tokenOut"] = token_out
        body["tokenOutChainId"] = chain_id()
    last_tx = ""
    gas_eth = 0.0
    try:
        data = _trade_post("/check_approval", body)
    except Exception as exc:  # noqa: BLE001
        data = {}
        log.info("approval check unread: %s", sanitize_exc(exc))
    for label in ("cancel", "revoke", "approval"):
        step = _tx_step(data.get(label) if isinstance(data, dict) else None)
        if step is None:
            continue
        sent = _confirmed_send(step, label=label)
        last_tx = str(sent.get("tx_id") or last_tx)
        gas_eth += float(sent.get("gas_eth") or 0)
        if not sent.get("ok"):
            return {
                "ok": False,
                "error": sent.get("error") or f"{label} failed",
                "tx_id": last_tx,
                "gas_eth": gas_eth,
            }
    need = int(amount or "0")
    allowed_ok, allowed = _permit2_allowance(token, who)
    if not allowed_ok:
        return {"ok": False, "error": "allowance unread", "tx_id": last_tx, "gas_eth": gas_eth}
    if allowed < max(need, 1):
        approve_tx = {
            "to": token,
            "data": _APPROVE + _pad_addr(PERMIT2) + _pad_int((1 << 256) - 1),
            "value": "0x0",
            "chainId": chain_id(),
        }
        sent = _confirmed_send(approve_tx, label="approve")
        last_tx = str(sent.get("tx_id") or last_tx)
        gas_eth += float(sent.get("gas_eth") or 0)
        if not sent.get("ok"):
            return {
                "ok": False,
                "error": sent.get("error") or "approve failed",
                "tx_id": last_tx,
                "gas_eth": gas_eth,
            }
    return {"ok": True, "tx_id": last_tx, "gas_eth": gas_eth}


def _permit2_allowance(token: str, owner: str) -> tuple[bool, int]:
    try:
        raw = eth_call(token, _ALLOWANCE + _pad_addr(owner) + _pad_addr(PERMIT2), timeout=8.0)
    except Exception as exc:  # noqa: BLE001
        log.info("allowance unread: %s", sanitize_exc(exc))
        return False, 0
    if not isinstance(raw, str) or len(raw) < 66:
        return False, 0
    return True, int(raw, 16)


def sell_slippage_pct() -> float:
    """Floor under a fresh sell quote. The fill is still the pool price."""
    try:
        return min(15.0, max(0.05, float(os.environ.get("RH_SELL_SLIPPAGE_PCT", "5"))))
    except ValueError:
        return 5.0


def sell_slippage_ladder() -> list[float]:
    """Try a fresh quote at each band. Only broadcast after eth_call succeeds."""
    base = sell_slippage_pct()
    out: list[float] = []
    for pct in (base, 5.0, 8.0, 12.0):
        p = min(15.0, max(0.05, float(pct)))
        if p not in out:
            out.append(p)
    return out


def _simulate_tx(tx: dict[str, Any]) -> tuple[bool, str]:
    """Dry-run via eth_call. False means do not broadcast (would burn gas and revert)."""
    to = str(tx.get("to") or "")
    data = str(tx.get("data") or "")
    if not to or not data.startswith("0x"):
        return False, "sim tx unread"
    who = wallet()
    if not who:
        return False, "RH_WALLET unread"
    try:
        eth_call(
            to,
            data,
            from_addr=who,
            value=tx.get("value") or "0x0",
            timeout=10.0,
        )
        return True, ""
    except Exception as exc:  # noqa: BLE001
        msg = sanitize_exc(exc)
        low = msg.lower()
        if any(x in low for x in ("execution reverted", "revert", "stf", "too little received", "slippage")):
            return False, msg
        # RPC unread (timeout / paid-plan) — still refuse sell broadcast so we do not burn gas blind.
        return False, f"sim unread · {msg}"


def plan_swap(
    *,
    token_in: str,
    token_out: str,
    amount_in: str,
    trade_type: str = "EXACT_INPUT",
    slippage_pct: float | None = None,
) -> dict[str, Any]:
    """Build unsigned swap via Uniswap Trading API for chain 4663."""
    who = wallet()
    base = {"ok": False, "send": False, "reason": "plan unread", "txs": []}
    if not who:
        base["reason"] = "RH_WALLET unread"
        return base
    if not _api_key():
        base["reason"] = "UNISWAP_API_KEY unread"
        return base
    try:
        quoted = _trade_post(
            "/quote",
            {
                "tokenIn": token_in,
                "tokenOut": token_out,
                "tokenInChainId": chain_id(),
                "tokenOutChainId": chain_id(),
                "amount": str(amount_in),
                "type": trade_type,
                "swapper": who,
                "slippageTolerance": (
                    float(os.environ.get("RH_SLIPPAGE_PCT", "1.0"))
                    if slippage_pct is None
                    else float(slippage_pct)
                ),
                "routingPreference": "BEST_PRICE",
            },
        )
    except Exception as exc:  # noqa: BLE001
        base["reason"] = f"quote failed · {sanitize_exc(exc)}"
        return base
    inner = quoted.get("quote") if isinstance(quoted, dict) else None
    quote_body = inner if isinstance(inner, dict) else quoted
    permit = quoted.get("permitData") if isinstance(quoted, dict) else None
    routing = str((quoted.get("routing") if isinstance(quoted, dict) else "") or "").upper()
    if routing.startswith("DUTCH"):
        base["reason"] = f"route {routing} · classic only"
        return base
    swap_body: dict[str, Any] = {
        "quote": quote_body,
        "swapper": who,
        "includeGasInfo": True,
        "refreshGasPrice": False,
    }
    if isinstance(permit, dict) and permit:
        try:
            swap_body["signature"] = _sign_permit(permit)
            swap_body["permitData"] = permit
        except Exception as exc:  # noqa: BLE001
            base["reason"] = f"permit sign failed · {sanitize_exc(exc)}"
            return base
    try:
        swap = _trade_post("/swap", swap_body)
    except Exception as exc:  # noqa: BLE001
        base["reason"] = f"swap build failed · {sanitize_exc(exc)}"
        return base
    tx = swap.get("swap") or swap.get("swapTransaction") or swap
    if not isinstance(tx, dict):
        base["reason"] = "swap tx unread"
        return base
    cid = _hex_int(tx.get("chainId"))
    if cid not in (0, chain_id()):
        base["reason"] = f"chainId {tx.get('chainId')} · expected {chain_id()}"
        return base
    data = str(tx.get("data") or "")
    if not data.startswith("0x") or len(data) < 10:
        base["reason"] = "swap calldata empty"
        return base
    return {"ok": True, "send": False, "reason": "plan ready", "tx": tx, "quote": quoted}


def _confirmed_send(tx: dict[str, Any], *, label: str, simulate: bool = False) -> dict[str, Any]:
    """Broadcast and wait. ok only when the receipt succeeded.

    When simulate=True, eth_call first. A would-be revert never hits the chain,
    so no gas is spent.
    """
    if simulate:
        ok_sim, why = _simulate_tx(tx)
        if not ok_sim:
            return {
                "ok": False,
                "error": f"{label} sim blocked · {why} · no broadcast · gas 0",
                "tx_id": "",
                "gas_eth": 0.0,
                "sim_blocked": True,
            }
    try:
        sent = _sign_and_send(tx)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{label} sign failed · {sanitize_exc(exc)}", "tx_id": "", "gas_eth": 0.0}
    tx_id = str(sent.get("tx_id") or "")
    if not sent.get("ok"):
        return {"ok": False, "error": f"{label} failed · {sent.get('error')}", "tx_id": tx_id, "gas_eth": 0.0}
    rec = wait_receipt(tx_id)
    if rec.get("pending") or not rec.get("ok"):
        why = rec.get("error") or "reverted"
        gas = float(rec.get("gas_eth") or 0)
        return {
            "ok": False,
            "error": f"{label} {why} · gas {gas:.6f} ETH · tx {tx_id}",
            "tx_id": tx_id,
            "gas_eth": gas,
        }
    return {"ok": True, "error": "", "tx_id": tx_id, "gas_eth": float(rec.get("gas_eth") or 0)}


def execute_buy(token: str, *, usdg_in: float | None = None) -> dict[str, Any]:
    """USDG → token. Approves Permit2 first when the allowance is missing."""
    size = float(usdg_in if usdg_in is not None else hard_cap())
    if size <= 0 or size > hard_cap() + 1e-9:
        return {"ok": False, "error": "size over hard cap", "tx_id": ""}
    amount = str(int(round(size * 1_000_000)))
    try:
        approval = ensure_token_approval(USDG, amount, token_out=token)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"approval failed · {sanitize_exc(exc)}", "tx_id": ""}
    if not approval.get("ok"):
        return {
            "ok": False,
            "error": approval.get("error") or "approval failed",
            "tx_id": approval.get("tx_id") or "",
            "gas_eth": float(approval.get("gas_eth") or 0),
        }
    try:
        plan = plan_swap(token_in=USDG, token_out=token, amount_in=amount)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"plan failed · {sanitize_exc(exc)}", "tx_id": "", "gas_eth": float(approval.get("gas_eth") or 0)}
    if not plan.get("ok"):
        return {"ok": False, "error": plan.get("reason") or "plan failed", "tx_id": "", "gas_eth": float(approval.get("gas_eth") or 0)}
    sent = _confirmed_send(plan["tx"], label="buy")
    sent["gas_eth"] = float(sent.get("gas_eth") or 0) + float(approval.get("gas_eth") or 0)
    sent["approval_gas_eth"] = float(approval.get("gas_eth") or 0)
    return sent


def execute_sell(token: str, *, token_amount: int = 0) -> dict[str, Any]:
    """Token → USDG. Simulate each ladder band; broadcast only when eth_call passes.

    If a broadcast still reverts (stale quote race), climb to the next slippage
    band with a fresh plan instead of aborting the whole sell.
    """
    amt = int(token_amount or 0)
    if amt <= 0:
        ok, amt = erc20_balance_read(token)
        if not ok:
            return {"ok": False, "error": "token balance unread", "tx_id": ""}
    if amt <= 0:
        return {"ok": False, "error": "token balance empty", "tx_id": ""}
    try:
        approval = ensure_token_approval(token, str(amt), token_out=USDG)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"approval failed · {sanitize_exc(exc)}", "tx_id": ""}
    if not approval.get("ok"):
        return {
            "ok": False,
            "error": approval.get("error") or "approval failed",
            "tx_id": approval.get("tx_id") or "",
            "gas_eth": float(approval.get("gas_eth") or 0),
        }
    gas_approval = float(approval.get("gas_eth") or 0)
    last_err = "sell plan unread"
    last_sent: dict[str, Any] = {}
    try:
        max_broadcasts = max(1, int(os.environ.get("RH_SELL_MAX_BROADCASTS", "2")))
    except ValueError:
        max_broadcasts = 2
    broadcasts = 0
    for slip in sell_slippage_ladder():
        try:
            plan = plan_swap(
                token_in=token,
                token_out=USDG,
                amount_in=str(amt),
                slippage_pct=slip,
            )
        except Exception as exc:  # noqa: BLE001
            last_err = f"plan failed · {sanitize_exc(exc)}"
            continue
        if not plan.get("ok"):
            last_err = str(plan.get("reason") or "plan failed")
            continue
        sent = _confirmed_send(plan["tx"], label="sell", simulate=True)
        sent["gas_eth"] = float(sent.get("gas_eth") or 0) + gas_approval
        sent["approval_gas_eth"] = gas_approval
        sent["slippage_pct"] = slip
        if sent.get("ok"):
            return sent
        if sent.get("sim_blocked"):
            # Would revert — try next slippage band; no chain gas spent.
            last_err = str(sent.get("error") or "sim blocked")
            continue
        # Broadcast happened and failed — climb ladder once more with fresh quote.
        broadcasts += 1
        last_sent = sent
        last_err = str(sent.get("error") or "sell reverted")
        if broadcasts >= max_broadcasts:
            return sent
        continue
    if last_sent:
        return last_sent
    return {
        "ok": False,
        "error": last_err,
        "tx_id": "",
        "gas_eth": gas_approval,
        "sim_blocked": True,
    }


def gas_refuel_enabled() -> bool:
    return os.environ.get("RH_GAS_REFUEL", "1").strip().lower() in ("1", "true", "yes", "on")


def gas_min_eth() -> float:
    """Below this, auto-refuel from USDG (when armed)."""
    try:
        return max(0.00005, float(os.environ.get("RH_GAS_MIN_ETH", "0.0004")))
    except ValueError:
        return 0.0004


def gas_refuel_usdg() -> float:
    """USDG spent per auto top-up. Clamped to 1–2."""
    try:
        return min(2.0, max(1.0, float(os.environ.get("RH_GAS_REFUEL_USDG", "1.5"))))
    except ValueError:
        return 1.5


def gas_refuel_floor_eth() -> float:
    """Need a crumb of ETH already to pay for the refuel txs themselves."""
    try:
        return max(0.00002, float(os.environ.get("RH_GAS_REFUEL_FLOOR_ETH", "0.00008")))
    except ValueError:
        return 0.00008


def execute_gas_refuel(*, usdg_in: float | None = None) -> dict[str, Any]:
    """USDG → WETH → unwrap native ETH. Only when ETH is below the min and send is armed."""
    from desk_realtime.rh_net import fetch_eth_balance

    if not send_armed():
        return {"ok": False, "error": "RH_UNI_SEND off · no refuel", "tx_id": ""}
    if not gas_refuel_enabled():
        return {"ok": False, "error": "RH_GAS_REFUEL off", "tx_id": "", "skipped": True}

    size = float(usdg_in if usdg_in is not None else gas_refuel_usdg())
    size = min(2.0, max(1.0, size))
    eth_now = float((fetch_eth_balance(timeout=5.0) or {}).get("eth") or 0.0)
    if eth_now >= gas_min_eth():
        return {
            "ok": True,
            "skipped": True,
            "reason": f"eth {eth_now:.6f} ≥ min {gas_min_eth():.6f}",
            "tx_id": "",
            "eth": eth_now,
        }
    if eth_now < gas_refuel_floor_eth():
        return {
            "ok": False,
            "error": (
                f"eth {eth_now:.6f} below floor {gas_refuel_floor_eth():.6f} · "
                "need manual ETH to pay for refuel tx"
            ),
            "tx_id": "",
            "eth": eth_now,
        }

    who = wallet()
    ok_u, raw_u = erc20_balance_read(USDG, who)
    usdg_bal = (float(raw_u) / 1_000_000.0) if ok_u else 0.0
    # Keep a little USDG for trading after the top-up.
    if usdg_bal < size + hard_cap():
        return {
            "ok": False,
            "error": f"USDG {usdg_bal:.2f} thin for refuel {size:.2f} + trade pad",
            "tx_id": "",
            "eth": eth_now,
        }

    amount = str(int(round(size * 1_000_000)))
    try:
        approval = ensure_token_approval(USDG, amount, token_out=WETH)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"approval failed · {sanitize_exc(exc)}", "tx_id": ""}
    gas_total = float(approval.get("gas_eth") or 0)
    if not approval.get("ok"):
        return {
            "ok": False,
            "error": approval.get("error") or "approval failed",
            "tx_id": approval.get("tx_id") or "",
            "gas_eth": gas_total,
        }

    _, weth_before = erc20_balance_read(WETH, who)
    try:
        plan = plan_swap(
            token_in=USDG,
            token_out=WETH,
            amount_in=amount,
            slippage_pct=float(os.environ.get("RH_GAS_REFUEL_SLIP_PCT", "1.0") or 1.0),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"plan failed · {sanitize_exc(exc)}",
            "tx_id": "",
            "gas_eth": gas_total,
        }
    if not plan.get("ok"):
        return {
            "ok": False,
            "error": plan.get("reason") or "plan failed",
            "tx_id": "",
            "gas_eth": gas_total,
        }

    sent = _confirmed_send(plan["tx"], label="gas_refuel", simulate=True)
    gas_total += float(sent.get("gas_eth") or 0)
    if not sent.get("ok"):
        sent["gas_eth"] = gas_total
        sent["usdg_in"] = size
        return sent

    _, weth_after = erc20_balance_read(WETH, who)
    delta = max(0, int(weth_after) - int(weth_before))
    unwrap_tx_id = ""
    if delta > 0:
        unwrap = {
            "to": WETH,
            "data": "0x2e1a7d4d" + _pad_int(delta),
            "value": "0x0",
            "gasLimit": hex(120_000),
        }
        unw = _confirmed_send(unwrap, label="weth_unwrap", simulate=True)
        gas_total += float(unw.get("gas_eth") or 0)
        unwrap_tx_id = str(unw.get("tx_id") or "")
        if not unw.get("ok"):
            return {
                "ok": False,
                "error": f"swap ok · unwrap failed · {unw.get('error')}",
                "tx_id": unwrap_tx_id or str(sent.get("tx_id") or ""),
                "swap_tx": str(sent.get("tx_id") or ""),
                "gas_eth": gas_total,
                "usdg_in": size,
                "weth_delta": delta,
            }

    eth_after = float((fetch_eth_balance(timeout=5.0) or {}).get("eth") or 0.0)
    return {
        "ok": True,
        "error": "",
        "tx_id": unwrap_tx_id or str(sent.get("tx_id") or ""),
        "swap_tx": str(sent.get("tx_id") or ""),
        "unwrap_tx": unwrap_tx_id,
        "gas_eth": gas_total,
        "usdg_in": size,
        "weth_delta": delta,
        "eth_before": eth_now,
        "eth_after": eth_after,
    }