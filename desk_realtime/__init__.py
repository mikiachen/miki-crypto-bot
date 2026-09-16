"""Real-time desk sync: WebSocket bus ↔ Streamlit UI (feed + panel + agents).

Trading engine runs in a separate process (`desk_realtime.trading_loop`).
UI only drains DeskBus / jsonl and writes IPC (`desk.halt` / `desk.panic` / `desk.valve`).
"""

from desk_realtime.bus import DeskBus
from desk_realtime.engine_state import (
    consume_panic,
    read_engine_state,
    request_panic,
    set_valve,
    valve_closed,
    write_engine_state,
)
from desk_realtime.fastforward import is_fastforward, ensure_tape
from desk_realtime.schema import normalize_ws_event, stage_packets

__all__ = [
    "DeskBus",
    "normalize_ws_event",
    "stage_packets",
    "read_engine_state",
    "write_engine_state",
    "request_panic",
    "consume_panic",
    "set_valve",
    "valve_closed",
    "is_fastforward",
    "ensure_tape",
]
