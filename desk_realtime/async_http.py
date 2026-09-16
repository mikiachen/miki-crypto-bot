"""Async HTTP helpers with hard timeouts (no sync blocks on Streamlit thread)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

_VENDOR = Path(__file__).resolve().parents[1] / "grok-trading-desk" / ".vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

from desk_realtime.secrets import redact_text, sanitize_exc

DEFAULT_TIMEOUT = 12.0


async def http_json(
    method: str,
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """
    Async JSON request via httpx. Always bounded by `timeout`.
    Raises TimeoutError / RuntimeError with redacted messages.
    """
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError("httpx missing — add grok-trading-desk/.vendor to PYTHONPATH") from exc

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method.upper(),
                url,
                json=json_body,
                headers=headers or {},
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                return data
            return {"data": data}
    except asyncio.CancelledError:
        raise
    except httpx.TimeoutException as exc:
        raise TimeoutError(f"HTTP timeout after {timeout:.1f}s: {redact_text(url)}") from None
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(sanitize_exc(exc)) from None


async def solana_rpc(
    rpc_url: str,
    method: str,
    params: list[Any] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """JSON-RPC call to a Solana endpoint with timeout."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or [],
    }
    data = await http_json("POST", rpc_url, json_body=body, timeout=timeout)
    if "error" in data:
        err = data["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise RuntimeError(f"rpc {method}: {redact_text(msg)}")
    return data.get("result")


async def llm_chat_json(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    timeout: float = 30.0,
) -> dict[str, Any]:
    """
    Async OpenAI-compatible chat completion → parsed JSON object.
    API key is sent in header only — never returned or logged.
    """
    if not api_key or api_key.startswith(("REPLACE", "xai-REPLACE", "sk-or-REPLACE")):
        raise RuntimeError("LLM api_key not configured")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # OpenRouter recommends these; harmless for other OpenAI-compatible hosts
        "HTTP-Referer": os.environ.get("OPENROUTER_REFERER", "http://localhost:8501"),
        "X-Title": os.environ.get("OPENROUTER_TITLE", "miki-crypto-desk"),
    }
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
    }
    data = await http_json(
        "POST",
        base_url,
        json_body=body,
        headers=headers,
        timeout=timeout,
    )
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("LLM response shape unexpected") from exc
    text = str(content or "").strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"raw": parsed}
    except json.JSONDecodeError:
        return {"text": redact_text(text), "score": 0.5}
