"""Uniswap on Arc: read-only quoter + optional local-signed Trading API swaps.

Official surfaces (chain 5042):
  Quoter 0x7dfd4f31…eac1468
  Universal Router 2.1.1 from sdk constants (watched live)
  ERC-20 USDC 0x3600…0000 is 6 decimals. Native gas USDC is 18 decimals.
Trade API builds unsigned calldata. Private key never leaves this machine.
Broadcast stays off until ARC_UNI_SEND=1. Hard cap 1 USDC. A tape pair is not a buy.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from typing import Any

from desk_realtime.foundry_bin import is_allowed_rpc, rpc_endpoints
from desk_realtime.secrets import sanitize_exc

log = logging.getLogger("arc_uniswap")

# @uniswap/sdk-core ChainId.ARC quoterAddress. Not the v2 router map.
QUOTER = "0x7dfd4f31be6814d2906bde155c3e1b146eac1468"
USDC = "0x3600000000000000000000000000000000000000"
_DECIMALS = "0x313ce567"
_QUOTE_V2 = "0xc6a5026a"
_FEES = (10000, 3000, 500, 100)
_MAX = 3
_ARC_CHAIN = 5042
_API_SPEC = "https://trade-api.gateway.uniswap.org/v1/api.json"
_ROUTER_SPEC = (
    "https://raw.githubusercontent.com/Uniswap/sdks/main/"
    "sdks/universal-router-sdk/src/utils/constants.ts"
)
_API_TTL = 600.0
_API_CACHE: dict[str, Any] = {}
_TRADE_API = "https://trade-api.gateway.uniswap.org/v1"
_HARD_CAP_USDC = 1.0


def send_armed() -> bool:
    return os.environ.get("ARC_UNI_SEND", "0").strip().lower() in ("1", "true", "yes", "on")


def _api_key() -> str:
    return (os.environ.get("UNISWAP_API_KEY") or "").strip()


def _wallet() -> str:
    return (os.environ.get("ARC_WALLET") or "").strip()


def _private_key() -> str:
    return (os.environ.get("ARC_PRIVATE_KEY") or "").strip()


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
            "User-Agent": "miki-desk-uni/1.0",
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
    return int(text)


def _validate_tx(tx: dict[str, Any], *, router: str) -> str:
    if not isinstance(tx, dict):
        return "tx unread"
    data = str(tx.get("data") or "").strip()
    if not data or data in ("0x", "0X"):
        return "empty calldata · no send"
    to = str(tx.get("to") or "").strip()
    if not (to.startswith("0x") and len(to) == 42):
        return "tx.to unread"
    if router and to.lower() != router.lower() and to.lower() != USDC.lower():
        # approval targets USDC; swap must hit Universal Router
        if data.startswith("0x095ea7b3"):
            pass
        else:
            return "tx.to is not Arc Universal Router · no send"
    if int(tx.get("chainId") or 0) not in (0, _ARC_CHAIN):
        return f"chainId {tx.get('chainId')} · expected {_ARC_CHAIN}"
    value = _hex_int(tx.get("value"))
    if value > int(_HARD_CAP_USDC * 1e18):
        return "native value over 1 USDC · no send"
    return ""


def _sign_permit(permit_data: dict[str, Any]) -> str:
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    pk = _private_key()
    if not pk:
        raise RuntimeError("ARC_PRIVATE_KEY not set")
    domain = permit_data.get("domain") or {}
    types = permit_data.get("types") or {}
    values = permit_data.get("values") or permit_data.get("message") or {}
    # eth-account expects full EIP-712 payload
    payload = {"types": types, "primaryType": "PermitSingle", "domain": domain, "message": values}
    if "PermitBatch" in types:
        payload["primaryType"] = "PermitBatch"
    try:
        signable = encode_typed_data(full_message=payload)
    except TypeError:
        signable = encode_typed_data(domain, types, values)
    signed = Account.from_key(pk).sign_message(signable)
    raw = signed.signature.hex() if hasattr(signed.signature, "hex") else str(signed.signature)
    raw = raw if raw.startswith("0x") else f"0x{raw}"
    return raw


def _broadcast_local(tx: dict[str, Any]) -> dict[str, Any]:
    """Sign and broadcast with the local key. Never posts the key to Uniswap."""
    from desk_realtime.arc_net import cast_send

    err = ""
    to = str(tx.get("to") or "")
    data = str(tx.get("data") or "")
    value = _hex_int(tx.get("value"))
    result = cast_send(to=to, value_wei=value, data=data, private_key=_private_key(), anti_snipe=True)
    if result.get("ok"):
        return result
    err = str(result.get("error") or "cast failed")
    # web3 fallback
    try:
        from web3 import Web3
        from eth_account import Account

        pk = _private_key()
        if not pk:
            return {"ok": False, "error": "ARC_PRIVATE_KEY not set", "tx_id": ""}
        rpc = next((u for u in rpc_endpoints() if is_allowed_rpc(u)), "")
        if not rpc:
            return {"ok": False, "error": err or "no rpc", "tx_id": ""}
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
        acct = Account.from_key(pk)
        built: dict[str, Any] = {
            "to": Web3.to_checksum_address(to),
            "from": acct.address,
            "data": data,
            "value": value,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "chainId": _ARC_CHAIN,
            "gas": int(tx.get("gasLimit") or tx.get("gas") or 400_000),
        }
        if tx.get("maxFeePerGas") and tx.get("maxPriorityFeePerGas"):
            built["maxFeePerGas"] = _hex_int(tx.get("maxFeePerGas"))
            built["maxPriorityFeePerGas"] = _hex_int(tx.get("maxPriorityFeePerGas"))
        elif tx.get("gasPrice"):
            built["gasPrice"] = _hex_int(tx.get("gasPrice"))
        else:
            tip = max(int(getattr(w3.eth, "max_priority_fee", 0) or 0), 20_000_000_000)
            base = int((w3.eth.get_block("latest") or {}).get("baseFeePerGas") or tip)
            built["maxPriorityFeePerGas"] = tip
            built["maxFeePerGas"] = max(base * 2 + tip, 22_000_000_000)
        signed = acct.sign_transaction(built)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = w3.eth.send_raw_transaction(raw)
        return {
            "ok": True,
            "tx_id": tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash),
            "source": "web3",
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": sanitize_exc(exc) or err, "tx_id": "", "source": "web3"}


def build_swap_plan(token_out: str, *, usdc_in: float = 0.5, symbol: str = "") -> dict[str, Any]:
    """USDC → token plan via Trade API. Private key is not sent."""
    amount = int(round(float(usdc_in) * 1_000_000))
    return _build_trade_plan(
        token_in=USDC,
        token_out=token_out,
        amount=str(amount),
        symbol=symbol,
        side="buy",
        usdc_in=float(usdc_in),
    )


def build_sell_plan(token_in: str, *, amount_token: int, symbol: str = "") -> dict[str, Any]:
    """Token → USDC plan. amount_token is raw ERC-20 units."""
    return _build_trade_plan(
        token_in=token_in,
        token_out=USDC,
        amount=str(int(amount_token)),
        symbol=symbol,
        side="sell",
        usdc_in=0.0,
    )


def _build_trade_plan(
    *,
    token_in: str,
    token_out: str,
    amount: str,
    symbol: str = "",
    side: str = "buy",
    usdc_in: float = 0.0,
) -> dict[str, Any]:
    token_in = (token_in or "").strip()
    token_out = (token_out or "").strip()
    wallet = _wallet()
    status = swap_api_status()
    base = {
        "ok": False,
        "send": False,
        "symbol": symbol,
        "token": token_out if side == "buy" else token_in,
        "side": side,
        "usdc_in": usdc_in,
        "reason": "plan unread",
    }
    if not status.get("listed") or not status.get("ok"):
        base["reason"] = status.get("reason") or "arc not listed"
        return base
    if not _api_key():
        base["reason"] = "UNISWAP_API_KEY not set"
        return base
    if not (wallet.startswith("0x") and len(wallet) == 42):
        base["reason"] = "ARC_WALLET unread"
        return base
    for label, addr in (("token_in", token_in), ("token_out", token_out)):
        if not (addr.startswith("0x") and len(addr) == 42):
            base["reason"] = f"{label} unread"
            return base
    if side == "buy":
        if usdc_in <= 0 or usdc_in > _HARD_CAP_USDC:
            base["reason"] = "size outside 0–1 USDC"
            return base
        if int(amount) <= 0 or int(amount) > 1_000_000:
            base["reason"] = "amount units outside 0–1 USDC"
            return base
    elif int(amount) <= 0:
        base["reason"] = "sell amount empty"
        return base
    try:
        quoted = _trade_post(
            "/quote",
            {
                "tokenIn": token_in,
                "tokenOut": token_out,
                "tokenInChainId": _ARC_CHAIN,
                "tokenOutChainId": _ARC_CHAIN,
                "amount": str(amount),
                "type": "EXACT_INPUT",
                "swapper": wallet,
                "slippageTolerance": float(os.environ.get("ARC_UNI_SLIPPAGE", "1.0")),
            },
        )
    except Exception as exc:  # noqa: BLE001
        base["reason"] = f"quote failed · {sanitize_exc(exc)}"
        return base
    routing = str(quoted.get("routing") or "")
    if routing != "CLASSIC":
        base["reason"] = f"routing {routing or 'unread'} · only CLASSIC local-sign · no send"
        return base
    quote_obj = quoted.get("quote")
    if not isinstance(quote_obj, dict):
        base["reason"] = "quote body unread"
        return base
    permit = quoted.get("permitData")
    signature = None
    if permit:
        try:
            signature = _sign_permit(permit if isinstance(permit, dict) else {})
        except Exception as exc:  # noqa: BLE001
            base["reason"] = f"permit sign failed · {sanitize_exc(exc)}"
            return base
    try:
        approval = _trade_post(
            "/check_approval",
            {
                "walletAddress": wallet,
                "token": token_in,
                "amount": str(amount),
                "chainId": _ARC_CHAIN,
            },
        )
    except Exception as exc:  # noqa: BLE001
        base["reason"] = f"approval check failed · {sanitize_exc(exc)}"
        return base
    swap_body: dict[str, Any] = {"quote": quote_obj}
    if permit and signature:
        swap_body["permitData"] = permit
        swap_body["signature"] = signature
    try:
        built = _trade_post("/swap", swap_body)
    except Exception as exc:  # noqa: BLE001
        base["reason"] = f"swap build failed · {sanitize_exc(exc)}"
        return base
    swap_tx = built.get("swap") if isinstance(built.get("swap"), dict) else None
    if not swap_tx:
        base["reason"] = "unsigned swap unread"
        return base
    router = str(status.get("router") or "")
    bad = _validate_tx(swap_tx, router=router)
    if bad:
        base["reason"] = bad
        return base
    if str(swap_tx.get("to") or "").lower() != router.lower():
        base["reason"] = "swap.to is not Arc Universal Router · no send"
        return base
    approval_tx = approval.get("approval") if isinstance(approval.get("approval"), dict) else None
    if approval_tx:
        bad_a = _validate_tx(approval_tx, router=router)
        if bad_a and ("empty" in bad_a or "over 1" in bad_a or "chainId" in bad_a):
            base["reason"] = f"approval · {bad_a}"
            return base
    out_hint = ""
    amount_out = quote_obj.get("amountOut")
    if isinstance(amount_out, dict):
        out_hint = str(amount_out.get("amount") or amount_out.get("minimumAmount") or "")
    return {
        "ok": True,
        "send": False,
        "symbol": symbol,
        "token": token_out if side == "buy" else token_in,
        "side": side,
        "usdc_in": round(usdc_in, 2) if side == "buy" else 0.0,
        "amount_in": str(amount),
        "routing": routing,
        "router": router,
        "request_id": built.get("requestId") or quoted.get("requestId"),
        "approval_tx": approval_tx,
        "swap_tx": {
            "to": swap_tx.get("to"),
            "data": swap_tx.get("data"),
            "value": swap_tx.get("value"),
            "chainId": swap_tx.get("chainId"),
            "gasLimit": swap_tx.get("gasLimit") or swap_tx.get("gas"),
            "maxFeePerGas": swap_tx.get("maxFeePerGas"),
            "maxPriorityFeePerGas": swap_tx.get("maxPriorityFeePerGas"),
            "gasPrice": swap_tx.get("gasPrice"),
            "from": swap_tx.get("from"),
        },
        "amount_out_hint": out_hint,
        "token_amount": int(out_hint) if str(out_hint).isdigit() else 0,
        "reason": (
            "plan ready · ARC_UNI_SEND off · no broadcast"
            if not send_armed()
            else "plan ready · send armed"
        ),
    }


def erc20_balance(token: str, owner: str = "") -> int:
    who = (owner or _wallet()).strip()
    token = (token or "").strip()
    if not (token.startswith("0x") and len(token) == 42 and who.startswith("0x") and len(who) == 42):
        return 0
    raw = _eth_call(token, "0x70a08231" + _pad_addr(who))
    if len(raw) < 66:
        return 0
    return int(raw[2:66], 16)


def pick_uni_scan_target(rng: Any = None) -> dict[str, str] | None:
    """Top Uniswap v3 tape row. Empty when send is off or tape unread."""
    if not send_armed():
        return None
    import random as _random

    rng = rng or _random.Random()
    try:
        from desk_realtime.arc_dex import sync_watch

        book = sync_watch()
    except Exception:
        return None
    rows = []
    for row in book.get("tape") or []:
        if str(row.get("dex") or "").lower() != "uniswap":
            continue
        labels = [str(x).lower() for x in (row.get("labels") or [])]
        if "v4" in labels:
            continue
        if labels and "v3" not in labels:
            continue
        addr = str(row.get("address") or "")
        sym = str(row.get("symbol") or "").upper()
        if not (addr.startswith("0x") and len(addr) == 42 and sym):
            continue
        if addr.lower() == USDC.lower():
            continue
        rows.append({"symbol": sym, "address": addr, "launchpad": "uniswap"})
    if not rows:
        return None
    return rng.choice(rows[:3])


def execute_swap_plan(plan: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Local-sign broadcast. Refuses unless ARC_UNI_SEND=1 and plan validates."""
    if plan is None:
        if kwargs.get("amount_token") is not None or str(kwargs.get("side") or "") == "sell":
            plan = build_sell_plan(
                str(kwargs.get("token_in") or kwargs.get("token") or ""),
                amount_token=int(kwargs.get("amount_token") or 0),
                symbol=str(kwargs.get("symbol") or ""),
            )
        else:
            plan = build_swap_plan(
                str(kwargs.get("token_out") or kwargs.get("token") or ""),
                usdc_in=float(kwargs.get("usdc_in") or 0.5),
                symbol=str(kwargs.get("symbol") or ""),
            )
    if not plan.get("ok"):
        return {**plan, "send": False}
    if not send_armed():
        return {**plan, "send": False, "reason": "ARC_UNI_SEND off · plan only · no broadcast"}
    if not _private_key():
        return {**plan, "ok": False, "send": False, "reason": "ARC_PRIVATE_KEY not set"}
    router = str(plan.get("router") or "")
    approval_tx = plan.get("approval_tx") if isinstance(plan.get("approval_tx"), dict) else None
    swap_tx = plan.get("swap_tx") if isinstance(plan.get("swap_tx"), dict) else None
    if not swap_tx:
        return {**plan, "ok": False, "send": False, "reason": "swap_tx unread"}
    bad = _validate_tx(swap_tx, router=router)
    if bad or str(swap_tx.get("to") or "").lower() != router.lower():
        return {
            **plan,
            "ok": False,
            "send": False,
            "reason": bad or "swap.to is not Arc Universal Router · no send",
        }
    txs: list[dict[str, Any]] = []
    if approval_tx and str(approval_tx.get("data") or "") not in ("", "0x"):
        sent_a = _broadcast_local(approval_tx)
        txs.append({"kind": "approval", **sent_a})
        if not sent_a.get("ok"):
            return {
                **plan,
                "ok": False,
                "send": True,
                "txs": txs,
                "reason": f"approval broadcast failed · {sent_a.get('error')}",
            }
        time.sleep(1.2)
    sent = _broadcast_local(swap_tx)
    txs.append({"kind": "swap", **sent})
    return {
        **plan,
        "ok": bool(sent.get("ok")),
        "send": True,
        "tx_id": sent.get("tx_id") or "",
        "txs": txs,
        "reason": (
            f"broadcast ok · {sent.get('tx_id')}"
            if sent.get("ok")
            else f"broadcast failed · {sent.get('error')}"
        ),
    }


def _pad_addr(addr: str) -> str:
    return addr.lower().replace("0x", "").rjust(64, "0")


def _pad_int(n: int) -> str:
    return f"{int(n):064x}"


def _eth_call(to: str, data: str) -> str:
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
    }).encode()
    timeout = float(os.environ.get("ARC_UNI_QUOTE_TIMEOUT", "6"))
    last: Exception | None = None
    for url in rpc_endpoints():
        if not is_allowed_rpc(url):
            continue
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json", "User-Agent": "miki-desk-uni/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode())
            if payload.get("error"):
                # A revert means this fee has no pool. Do not walk every backup RPC.
                return ""
            raw = payload.get("result")
            if not isinstance(raw, str):
                raise RuntimeError("empty quote")
            return raw
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError(sanitize_exc(last) if last else "quote rpc failed")


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


def usdc_decimals() -> int:
    raw = _eth_call(USDC, _DECIMALS)
    if len(raw) < 66:
        return 0
    return int(raw, 16)


def quote_roundtrip(token: str, *, usdc_in: float = 0.5) -> dict[str, Any]:
    """Buy then sell quote for one v3 token. Empty amounts are not a fill."""
    token = (token or "").strip()
    base = {
        "ok": False,
        "token": token,
        "usdc_in": usdc_in,
        "usdc_out": 0.0,
        "fee": 0,
        "send": False,
        "reason": "quote unread",
    }
    if not (token.startswith("0x") and len(token) == 42):
        base["reason"] = "token unread"
        return base
    if token.lower() == USDC:
        base["reason"] = "quote token is USDC"
        return base
    try:
        decimals = usdc_decimals()
    except Exception as exc:  # noqa: BLE001
        base["reason"] = f"usdc decimals unread · {sanitize_exc(exc)}"
        return base
    if decimals != 6:
        base["reason"] = f"usdc decimals {decimals} · expected 6 · no send"
        return base
    amount_in = int(round(float(usdc_in) * 1_000_000))
    if amount_in <= 0 or amount_in > 1_000_000:
        base["reason"] = "quote size outside 0–1 USDC"
        return base
    best: tuple[int, int, int] | None = None
    for fee in _FEES:
        try:
            bought = _quote_call(USDC, token, amount_in, fee)
        except Exception:
            bought = 0
        if bought <= 0:
            continue
        try:
            sold = _quote_call(token, USDC, bought, fee)
        except Exception:
            sold = 0
        if best is None or sold > best[2]:
            best = (fee, bought, sold)
    if not best or best[2] <= 0:
        base["reason"] = "v3 quote empty · no send"
        return base
    fee, bought, sold = best
    usdc_out = sold / 1_000_000
    return {
        "ok": True,
        "token": token,
        "usdc_in": round(amount_in / 1_000_000, 2),
        "usdc_out": round(usdc_out, 6),
        "tokens_out": bought,
        "fee": fee,
        "send": False,
        "reason": f"quote {amount_in / 1_000_000:.2f}→{usdc_out:.4f} fee {fee}",
    }


def quote_held_mark(token: str, *, token_amount: int = 0, cost_usdc: float = 0.0) -> dict[str, Any]:
    """Mark a Uniswap bag by quoting the held token back to USDC. No broadcast."""
    token = (token or "").strip()
    amt = int(token_amount or 0)
    if amt <= 0 and token.startswith("0x"):
        try:
            amt = erc20_balance(token)
        except Exception:
            amt = 0
    empty = {
        "token": token,
        "token_amount": amt,
        "mark_usdc": 0.0,
        "live_mult": 0.0,
        "source": "none",
    }
    if amt <= 0 or not (token.startswith("0x") and len(token) == 42):
        return empty
    best = 0
    for fee in _FEES:
        try:
            sold = _quote_call(token, USDC, amt, fee)
        except Exception:
            sold = 0
        if sold > best:
            best = sold
    if best <= 0:
        return empty
    mark_usdc = best / 1_000_000
    cost = float(cost_usdc or 0.0)
    mult = (mark_usdc / cost) if cost > 0 and mark_usdc > 0 else 0.0
    return {
        "token": token,
        "token_amount": amt,
        "mark_usdc": round(mark_usdc, 6),
        "live_mult": round(mult, 4) if mult > 0 else 0.0,
        "source": "uniQuote",
    }


def _https_text(url: str, timeout: float = 12.0) -> str:
    if not url.startswith("https://"):
        raise RuntimeError("api list is not https")
    req = urllib.request.Request(url, headers={"User-Agent": "miki-desk-uni/1.0", "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode(errors="replace")


def _chain_in_trade_api(spec: str) -> bool:
    data = json.loads(spec)
    schemas = (data.get("components") or {}).get("schemas") or {}
    for schema in schemas.values():
        if not isinstance(schema, dict):
            continue
        enum = schema.get("enum")
        if not isinstance(enum, list):
            continue
        ids = {int(x) for x in enum if isinstance(x, int) or (isinstance(x, str) and x.isdigit())}
        if _ARC_CHAIN in ids and 1 in ids and 4663 in ids:
            return True
    return False


def _router_from_sdk(text: str) -> str:
    start = text.find("// arc")
    if start < 0:
        return ""
    block = text[start:start + 500]
    match = re.search(r"address:\s*'(0x[0-9a-fA-F]{40})'", block)
    return match.group(1) if match else ""


def _code_present(addr: str) -> bool:
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getCode",
        "params": [addr, "latest"],
    }).encode()
    for url in rpc_endpoints():
        if not is_allowed_rpc(url):
            continue
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json", "User-Agent": "miki-desk-uni/1.0"},
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                payload = json.loads(resp.read().decode())
            raw = str(payload.get("result") or "")
            if raw not in ("", "0x", "0x0") and len(raw) > 10:
                return True
        except Exception:
            continue
    return False


def swap_api_status(*, force: bool = False) -> dict[str, Any]:
    """Official Trading API allow-list. No key, no signature, no broadcast."""
    now = time.time()
    hit = _API_CACHE.get("row")
    if hit and not force and now - float(hit.get("ts") or 0) < _API_TTL:
        return hit
    row: dict[str, Any] = {
        "ok": False,
        "listed": False,
        "chain_id": _ARC_CHAIN,
        "router": "",
        "has_key": bool((os.environ.get("UNISWAP_API_KEY") or "").strip()),
        "send": False,
        "reason": "api list unread",
        "ts": now,
    }
    try:
        listed = _chain_in_trade_api(_https_text(_API_SPEC))
        router = _router_from_sdk(_https_text(_ROUTER_SPEC))
    except Exception as exc:  # noqa: BLE001
        row["reason"] = f"api list unread · {sanitize_exc(exc)}"
        if hit:
            stale = dict(hit)
            stale["reason"] = row["reason"]
            stale["ts"] = now
            return stale
        _API_CACHE["row"] = row
        return row
    if not listed:
        row["reason"] = "arc 5042 absent · no send"
        _API_CACHE["row"] = row
        return row
    if not router or not _code_present(router):
        row["listed"] = True
        row["reason"] = "arc listed · router unread · no send"
        _API_CACHE["row"] = row
        return row
    row.update({
        "ok": True,
        "listed": True,
        "router": router,
        "send": False,
        "reason": (
            "arc listed · api key set · send armed · still gate-bound"
            if row["has_key"] and send_armed()
            else (
                "arc listed · api key set · quote only · no send"
                if row["has_key"]
                else "arc listed · no api key · no send"
            )
        ),
    })
    _API_CACHE["row"] = row
    return row


def quote_tape(rows: list[dict[str, Any]], *, limit: int = _MAX) -> list[dict[str, Any]]:
    """Quote the largest Uniswap v3 tape rows. v4 stays unread. No broadcast."""
    out: list[dict[str, Any]] = []
    for row in rows:
        if len(out) >= limit:
            break
        if str(row.get("dex") or "").lower() != "uniswap":
            continue
        labels = [str(x).lower() for x in (row.get("labels") or [])]
        sym = str(row.get("symbol") or "")
        if "v4" in labels:
            out.append({
                "ok": False,
                "symbol": sym,
                "send": False,
                "reason": "v4 pool · quote unread · no send",
            })
            continue
        if labels and "v3" not in labels:
            continue
        quoted = quote_roundtrip(str(row.get("address") or ""))
        quoted["symbol"] = sym
        out.append(quoted)
    return out
