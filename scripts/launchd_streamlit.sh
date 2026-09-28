#!/bin/bash
export HOME="/Users/miki"
export PATH="/Users/miki/Library/Python/3.9/bin:/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"
ROOT="/Users/miki/ai-quant-researcher-main"
export PYTHONPATH="${ROOT}/grok-trading-desk/.vendor:${ROOT}"
set -a
[ -f "/Users/miki/Library/Application Support/miki-desk/.env" ] && . "/Users/miki/Library/Application Support/miki-desk/.env" || true
[ -f "/Users/miki/Library/Application Support/miki-desk/.env.rh" ] && . "/Users/miki/Library/Application Support/miki-desk/.env.rh" || true
set +a
export DESK_CHAIN=robinhood
cd "${ROOT}" || exit 1
exec /usr/bin/python3 -m streamlit run "${ROOT}/miki_crypto_app.py"   --server.port 8501   --server.address 0.0.0.0   --server.headless true   --browser.gatherUsageStats false
