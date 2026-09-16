#!/usr/bin/env bash
# Grok trading loop → desk WS hub (start server first)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}/grok-trading-desk/.vendor:${ROOT}:${PYTHONPATH:-}"
exec python3 -m desk_realtime.trading_loop "$@"
