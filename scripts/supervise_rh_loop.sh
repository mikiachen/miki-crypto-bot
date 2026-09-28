#!/usr/bin/env bash
# Robinhood Chain short-term desk. Does NOT start Arc.
# Usage:
#   ./scripts/supervise_rh_loop.sh
#   ./scripts/supervise_rh_loop.sh --live   # still needs RH_UNI_SEND=1 in env
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
if [[ -f "${ROOT}/.env.rh" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/.env.rh" 2>/dev/null || true
  set +a
fi

export DESK_CHAIN=robinhood
# Keep Arc cold.
unset ARC_FORCE_BROADCAST ARC_LIVE ARC_UNI_SEND 2>/dev/null || true
export ARC_FORCE_BROADCAST=0
export ARC_LIVE=0
export ARC_UNI_SEND=0

export RH_UNI_SEND="${RH_UNI_SEND:-0}"
export RH_BET_HARD_CAP="${RH_BET_HARD_CAP:-1.5}"
export RH_UNIVERSE="${RH_UNIVERSE:-liquid}"
export RH_MAX_HOLD_SEC="${RH_MAX_HOLD_SEC:-21600}"
export RH_HARD_STOP="${RH_HARD_STOP:-0.97}"
export RH_TAKE_PROFIT="${RH_TAKE_PROFIT:-1.04}"
export RH_MIN_M5_PCT="${RH_MIN_M5_PCT:-0.35}"
export RH_MIN_ROUNDTRIP="${RH_MIN_ROUNDTRIP:-0.97}"
export RH_REENTRY_SEC="${RH_REENTRY_SEC:-1800}"
export RH_MIN_LIQ_USDG="${RH_MIN_LIQ_USDG:-100000}"
export RH_SELL_SLIPPAGE_PCT="${RH_SELL_SLIPPAGE_PCT:-5}"
RH_SCAN_EVERY="${RH_SCAN_EVERY:-8}"
RH_EXIT_EVERY="${RH_EXIT_EVERY:-3}"

# Pump intel defaults (radar + short trade tape). CLI flags override; ignore stale shell.
export PUMP_INTEL=1
export PUMP_NARRATIVE_LLM="${PUMP_NARRATIVE_LLM:-0}"
export PUMP_WATCH=1
export PUMP_WATCH_SEC="${PUMP_WATCH_SEC:-75}"
export PUMP_WATCH_MAX="${PUMP_WATCH_MAX:-12}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --live) export RH_UNI_SEND=1; shift ;;
    --cap)
      export RH_BET_HARD_CAP="$2"
      shift 2
      ;;
    --no-pump) export PUMP_INTEL=0; shift ;;
    --pump-llm) export PUMP_NARRATIVE_LLM=1; shift ;;
    --no-watch) export PUMP_WATCH=0; shift ;;
    *) shift ;;
  esac
done

# Ensure valve starts panic-closed until operator opens (safety).
VALVE="${ROOT}/grok-trading-desk/logs/desk.valve"
if [[ ! -f "$VALVE" ]]; then
  echo "CLOSED panic" >"$VALVE"
fi

WS_LOG="${ROOT}/grok-trading-desk/logs/ws_hub.log"
LOOP_LOG="${ROOT}/grok-trading-desk/logs/rh_loop.log"
PUMP_LOG="${ROOT}/grok-trading-desk/logs/pump_intel.log"
echo "supervise rh · send=${RH_UNI_SEND} cap=${RH_BET_HARD_CAP} hold=${RH_MAX_HOLD_SEC}s scan=${RH_SCAN_EVERY}s exit=${RH_EXIT_EVERY}s"
echo "  ws   → ${WS_LOG}"
echo "  loop → ${LOOP_LOG}"
echo "  pump → ${PUMP_LOG} (intel=${PUMP_INTEL} watch=${PUMP_WATCH} ${PUMP_WATCH_SEC}s)"
echo "  Arc  → OFF"

# Never leave Arc trading_loop running alongside RH.
pkill -f 'desk_realtime.trading_loop' 2>/dev/null || true
pkill -f 'desk_realtime.rh_loop' 2>/dev/null || true
pkill -f 'desk_realtime.pump_intel' 2>/dev/null || true

ensure_ws() {
  if lsof -tiTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
    return 0
  fi
  echo "$(date '+%F %T') restart ws hub" | tee -a "$WS_LOG"
  nohup python3 -m desk_realtime.server --host 127.0.0.1 --port 8765 >>"$WS_LOG" 2>&1 &
  sleep 1
}

ensure_loop() {
  if pgrep -f 'desk_realtime.rh_loop' >/dev/null 2>&1; then
    return 0
  fi
  echo "$(date '+%F %T') restart rh_loop" | tee -a "$LOOP_LOG"
  nohup python3 -m desk_realtime.rh_loop \
    --ws ws://127.0.0.1:8765 \
    --scan-every "${RH_SCAN_EVERY}" \
    --exit-every "${RH_EXIT_EVERY}" >>"$LOOP_LOG" 2>&1 &
  sleep 1
}

ensure_pump() {
  if [[ "${PUMP_INTEL}" != "1" && "${PUMP_INTEL}" != "true" && "${PUMP_INTEL}" != "yes" && "${PUMP_INTEL}" != "on" ]]; then
    return 0
  fi
  if pgrep -f 'desk_realtime.pump_intel' >/dev/null 2>&1; then
    return 0
  fi
  echo "$(date '+%F %T') restart pump_intel (read-only)" | tee -a "$PUMP_LOG"
  # PYTHONPATH must include grok-trading-desk for src.crypto.scout
  nohup env PYTHONPATH="${ROOT}/grok-trading-desk:${ROOT}/grok-trading-desk/.vendor:${ROOT}:${PYTHONPATH:-}" \
    python3 -m desk_realtime.pump_intel >>"$PUMP_LOG" 2>&1 &
  sleep 1
}

ensure_ws
ensure_loop
ensure_pump

while true; do
  ensure_ws
  ensure_loop
  ensure_pump
  # Guard: if someone starts Arc loop, kill it.
  if pgrep -f 'desk_realtime.trading_loop' >/dev/null 2>&1; then
    echo "$(date '+%F %T') kill stray Arc trading_loop" | tee -a "$LOOP_LOG"
    pkill -f 'desk_realtime.trading_loop' 2>/dev/null || true
  fi
  sleep 5
done
