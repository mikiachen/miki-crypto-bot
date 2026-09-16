#!/usr/bin/env bash
# Desk realtime WebSocket hub
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}/grok-trading-desk/.vendor:${ROOT}:${PYTHONPATH:-}"
exec python3 -m desk_realtime.server "$@"
