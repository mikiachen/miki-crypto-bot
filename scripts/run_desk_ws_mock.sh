#!/usr/bin/env bash
# Push the 5-stage mock sequence into the desk WS hub
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}/grok-trading-desk/.vendor:${ROOT}:${PYTHONPATH:-}"
exec python3 -m desk_realtime.mock_sequence "$@"
