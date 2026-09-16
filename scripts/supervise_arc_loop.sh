#!/usr/bin/env bash
# Keep WS hub (:8765) + trading_loop alive. Restarts on crash.
# Usage:
#   ./scripts/supervise_arc_loop.sh
#   ./scripts/supervise_arc_loop.sh --live
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p grok-trading-desk/logs

export PATH="${ROOT}:${PATH}"
export PYTHONPATH="${ROOT}/grok-trading-desk/.vendor:${ROOT}:${PYTHONPATH:-}"

if [[ -f "${ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/.env" 2>/dev/null || true
  set +a
fi

export DESK_CHAIN=arc
export ARC_PADS="${ARC_PADS:-warp}"
export ARC_ALLOW_PLACEHOLDERS="${ARC_ALLOW_PLACEHOLDERS:-0}"
export ARC_FORCE_BROADCAST="${ARC_FORCE_BROADCAST:-0}"
export ARC_LIVE="${ARC_LIVE:-0}"
export ARC_SKIP_BALANCE_CHECK="${ARC_SKIP_BALANCE_CHECK:-0}"

STAKE="${STAKE:-0.5}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --live) export ARC_FORCE_BROADCAST=1 ARC_LIVE=1; shift ;;
    --stake) STAKE="$2"; shift 2 ;;
    *) shift ;;
  esac
done

WS_LOG="${ROOT}/grok-trading-desk/logs/ws_hub.log"
LOOP_LOG="${ROOT}/grok-trading-desk/logs/trading_loop.log"
echo "supervise · pads=${ARC_PADS} live=${ARC_LIVE} stake=${STAKE}"
echo "  ws   → ${WS_LOG}"
echo "  loop → ${LOOP_LOG}"

ensure_ws() {
  if lsof -tiTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
    return 0
  fi
  echo "$(date '+%F %T') restart ws hub" | tee -a "$WS_LOG"
  nohup python3 -m desk_realtime.server --host 127.0.0.1 --port 8765 >>"$WS_LOG" 2>&1 &
  sleep 1
}

ensure_loop() {
  if pgrep -f 'desk_realtime.trading_loop' >/dev/null 2>&1; then
    return 0
  fi
  echo "$(date '+%F %T') restart trading_loop" | tee -a "$LOOP_LOG"
  nohup python3 -m desk_realtime.trading_loop \
    --ws ws://127.0.0.1:8765 \
    --stake "${STAKE}" \
    --scan-every 3 \
    --exit-every 1.5 >>"$LOOP_LOG" 2>&1 &
  sleep 1
}

# initial start
pkill -f 'desk_realtime.trading_loop' 2>/dev/null || true
ensure_ws
ensure_loop

while true; do
  ensure_ws
  ensure_loop
  sleep 5
done
