"""Mock the five-stage live packet sequence against the desk WS server.

Usage:
  # terminal A
  PYTHONPATH=grok-trading-desk/.vendor:. python -m desk_realtime.server

  # terminal B
  PYTHONPATH=grok-trading-desk/.vendor:. python -m desk_realtime.mock_sequence

  # or local inject (no server) from Streamlit debug buttons
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

_VENDOR = Path(__file__).resolve().parents[1] / "grok-trading-desk" / ".vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

from desk_realtime.schema import stage_packets


async def run(url: str, token: str, delay: float) -> None:
    import websockets

    packets = stage_packets(token)
    labels = ["INIT→HOLD_OFF", "VOTING", "VETO→STOPPED_OUT", "BUY/OPEN", "EXIT·4x"]
    async with websockets.connect(url) as ws:
        print(f"[mock] connected {url}", flush=True)
        for i, (pkt, label) in enumerate(zip(packets, labels), start=1):
            pkt = dict(pkt)
            pkt["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
            await ws.send(json.dumps(pkt, ensure_ascii=False))
            print(f"[mock] stage {i}/5 {label}: {pkt.get('status')} {pkt.get('token_name')}", flush=True)
            await asyncio.sleep(delay)
        print("[mock] sequence complete", flush=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://127.0.0.1:8765")
    p.add_argument("--token", default="SOLGOD")
    p.add_argument("--delay", type=float, default=1.2, help="seconds between stages")
    args = p.parse_args()
    asyncio.run(run(args.url, args.token, args.delay))


if __name__ == "__main__":
    main()
