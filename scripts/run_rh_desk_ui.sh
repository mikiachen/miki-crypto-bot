#!/usr/bin/env bash
# Keep Streamlit RH desk UI on 127.0.0.1:8501 (+ RH supervise if down).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p grok-trading-desk/logs
export PYTHONPATH="${ROOT}/grok-trading-desk/.vendor:${ROOT}:${PYTHONPATH:-}"

if [[ -f "${ROOT}/.env" ]]; then set -a; source "${ROOT}/.env" 2>/dev/null || true; set +a; fi
if [[ -f "${ROOT}/.env.rh" ]]; then set -a; source "${ROOT}/.env.rh" 2>/dev/null || true; set +a; fi
export DESK_CHAIN=robinhood

if ! pgrep -f 'supervise_rh_loop.sh' >/dev/null 2>&1; then
  nohup ./scripts/supervise_rh_loop.sh >>grok-trading-desk/logs/supervise_rh.log 2>&1 &
fi

UI_LOG="${ROOT}/grok-trading-desk/logs/streamlit_desk.log"
while true; do
  if ! lsof -tiTCP:8501 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "$(date '+%F %T') restart streamlit" | tee -a "$UI_LOG"
    # Prefer system/python3 streamlit; fall back to module
    nohup python3 -m streamlit run miki_crypto_app.py \
      --server.port 8501 \
      --server.address 0.0.0.0 \
      --server.headless true \
      --browser.gatherUsageStats false \
      >>"$UI_LOG" 2>&1 &
    sleep 3
  fi
  sleep 5
done
