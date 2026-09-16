#!/usr/bin/env bash
# Recording-only desk: 8h mission replayed in ~5 minutes (no live WS / RPC).
# Usage:
#   ./scripts/run_fastforward_desk.sh
#   ./scripts/run_fastforward_desk.sh --port 8502
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export DESK_FASTFORWARD=1
export DESK_WS_ENABLED=0
export DESK_PAPER=0
export DESK_FF_UI_PAUSE="${DESK_FF_UI_PAUSE:-0.05}"
export PYTHONPATH="${ROOT}/grok-trading-desk/.vendor:${ROOT}:${PYTHONPATH:-}"

PORT=8501
EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --regen)
      python3 -m desk_realtime.fastforward --force
      shift
      ;;
    *) EXTRA+=("$1"); shift ;;
  esac
done

# Ensure tape exists (100 tokens, ~1100 events, $ZZZ moonshot)
python3 -m desk_realtime.fastforward

echo "═══════════════════════════════════════════════════"
echo "  miki desk · FASTFORWARD (录屏加速版)"
echo "  8h tape → ~5 min · UI pause ${DESK_FF_UI_PAUSE}s"
echo "  http://127.0.0.1:${PORT}"
echo "═══════════════════════════════════════════════════"

exec python3 -m streamlit run miki_crypto_app.py \
  --server.port "${PORT}" \
  --server.headless true \
  --browser.gatherUsageStats false \
  "${EXTRA[@]+"${EXTRA[@]}"}"
