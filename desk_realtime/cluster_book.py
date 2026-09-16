"""Shared holder graph for narrative (agent 2) and cluster risk (agent 4).

Both sides read/write the same JSON book. Optional Goldsky query URL can
refresh it; otherwise recent Arc Transfer logs fill the same structure.
No invented scores when the chain returns nothing.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_BOOK = _ROOT / "grok-trading-desk" / "logs" / "cluster_book.json"
_TTL = float(os.environ.get("ARC_CLUSTER_BOOK_TTL", "20"))
_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _load() -> dict[str, Any]:
    if not _BOOK.is_file():
        return {"tokens": {}, "ts": 0.0, "source": "empty"}
    try:
        raw = json.loads(_BOOK.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {"tokens": {}, "ts": 0.0}
    except Exception:
        return {"tokens": {}, "ts": 0.0, "source": "empty"}


def _save(book: dict[str, Any]) -> None:
    _BOOK.parent.mkdir(parents=True, exist_ok=True)
    tmp = _BOOK.with_suffix(".tmp")
    tmp.write_text(json.dumps(book), encoding="utf-8")
    tmp.replace(_BOOK)


def publish_narrative(token: str, address: str, pulse: dict[str, Any] | None) -> None:
    """Agent 2 writes the latest narrative pulse into the shared book."""
    book = _load()
    tokens = book.setdefault("tokens", {})
    key = (address or token or "").lower()
    if not key:
        return
    row = tokens.get(key) if isinstance(tokens.get(key), dict) else {}
    pulse = pulse or {}
    row.update(
        {
            "token": token,
            "address": address,
            "mentions": pulse.get("mention_count"),
            "engagement": pulse.get("engagement"),
            "narrative_ts": time.time(),
        }
    )
    tokens[key] = row
    book["ts"] = time.time()
    _save(book)


def shared_cluster(token: str, address: str) -> dict[str, Any]:
    """Agent 4 reads the same book, refreshing transfers if stale."""
    book = _load()
    key = (address or "").lower()
    tokens = book.get("tokens") if isinstance(book.get("tokens"), dict) else {}
    row = tokens.get(key) if key and isinstance(tokens.get(key), dict) else {}
    age = time.time() - float(row.get("transfer_ts") or 0)
    if key and age > _TTL:
        fresh = _refresh_transfers(address)
        if fresh:
            row = {**row, **fresh, "token": token, "address": address}
            tokens[key] = row
            book["tokens"] = tokens
            book["ts"] = time.time()
            book["source"] = fresh.get("source") or "rpc_transfers"
            _save(book)
    from_addrs = row.get("from_counts") if isinstance(row.get("from_counts"), dict) else {}
    total = sum(int(v) for v in from_addrs.values()) or 0
    top = max((int(v) for v in from_addrs.values()), default=0)
    score = (top / total) if total else None
    return {
        "token": token,
        "address": address,
        "cluster_score": None if score is None else round(score, 3),
        "top_share": None if score is None else round(score, 3),
        "transfer_count": total,
        "mentions": row.get("mentions"),
        "source": row.get("source") or book.get("source") or "empty",
        "note": row.get("note") or "",
    }


def _refresh_transfers(address: str) -> dict[str, Any] | None:
    goldsky = (os.environ.get("GOLDSKY_QUERY_URL") or "").strip()
    if goldsky.startswith("https://"):
        got = _goldsky(goldsky, address)
        if got:
            return got
    return _rpc_transfers(address)


def _goldsky(url: str, address: str) -> dict[str, Any] | None:
    import urllib.request

    query = {
        "query": "query($id: String!) { token(id: $id) { id } }",
        "variables": {"id": address.lower()},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(query).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "miki-cluster/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("errors"):
        return {"source": "goldsky", "note": "subgraph has no Arc token yet", "from_counts": {}, "transfer_ts": time.time()}
    return {"source": "goldsky", "from_counts": {}, "transfer_ts": time.time(), "note": "goldsky reachable"}


def _rpc_transfers(address: str) -> dict[str, Any] | None:
    from desk_realtime.arc_net import _endpoint_order

    import urllib.request

    def _rpc(rpc: str, method: str, params: list) -> Any:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(
            rpc,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "miki-cluster/1.0"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        if not isinstance(data, dict) or data.get("error"):
            raise RuntimeError(str((data or {}).get("error") or "rpc")[:80])
        return data.get("result")

    last = ""
    for rpc in _endpoint_order():
        if not str(rpc).startswith("https://"):
            continue
        try:
            head = int(_rpc(rpc, "eth_blockNumber", []), 16)
            start = max(0, head - 4000)
            logs = _rpc(
                rpc,
                "eth_getLogs",
                [{
                    "address": address,
                    "topics": [_TRANSFER],
                    "fromBlock": hex(start),
                    "toBlock": "latest",
                }],
            )
        except Exception as exc:
            last = type(exc).__name__
            continue
        if not isinstance(logs, list):
            last = "no logs"
            continue
        counts: dict[str, int] = {}
        for row in logs[-80:]:
            topics = row.get("topics") if isinstance(row, dict) else None
            if not topics or len(topics) < 2:
                continue
            sender = "0x" + str(topics[1])[-40:]
            counts[sender.lower()] = counts.get(sender.lower(), 0) + 1
        return {
            "source": "rpc_transfers",
            "from_counts": counts,
            "transfer_ts": time.time(),
            "note": last,
            "rpc": rpc,
        }
    return {
        "source": "rpc_transfers",
        "from_counts": {},
        "transfer_ts": time.time(),
        "note": last or "no transfer logs",
    }
