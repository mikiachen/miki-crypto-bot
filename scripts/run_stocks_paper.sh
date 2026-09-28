#!/usr/bin/env bash
# US equities desk (Robinhood-style) — Alpaca paper, stocks only.
# Does not start Arc / pump.fun. Does not touch miki_crypto_app.py.
#
# Need in env or grok-trading-desk/config.yaml:
#   GROK_API_KEY (or xai key in yaml)
#   ALPACA_API_KEY
#   ALPACA_API_SECRET
#
# Usage:
#   ./scripts/run_stocks_paper.sh           # paper, dry-run (no broker orders)
#   ./scripts/run_stocks_paper.sh --orders  # paper, real Alpaca paper brackets
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DESK="${ROOT}/grok-trading-desk"
cd "$DESK"

if [[ -f "${ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/.env" 2>/dev/null || true
  set +a
fi

export PYTHONPATH="${DESK}/.vendor:${ROOT}:${PYTHONPATH:-}"
mkdir -p logs

ORDERS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --orders) ORDERS=1; shift ;;
    *) shift ;;
  esac
done

echo "═══════════════════════════════════════════════════"
echo "  miki stocks · Alpaca paper · stocks-only"
echo "  RTH America/New_York 09:35–15:55 weekdays"
echo "  log → ${DESK}/logs/desk.jsonl"
echo "  watch → python3 scripts/dashboard.py --log logs/desk.jsonl"
if [[ "${ORDERS}" == "1" ]]; then
  echo "  mode · paper orders ON"
else
  echo "  mode · dry-run (decide + log, no broker)"
fi
echo "═══════════════════════════════════════════════════"

if [[ "${ORDERS}" == "1" ]]; then
  exec python3 -m src.desk --config config.yaml --stocks-only
fi
exec python3 -m src.desk --config config.yaml --stocks-only --dry-run
