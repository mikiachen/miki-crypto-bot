#!/usr/bin/env bash
# Start the official desk in dry-run (no real orders).
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PWD}/.vendor:${PYTHONPATH:-}"
mkdir -p logs
echo "▶ grok-trading-desk dry-run  (CTRL+C to stop)"
echo "  config=config.yaml  log=logs/desk.jsonl"
exec python3 -m src.desk --config config.yaml --dry-run
