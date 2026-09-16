#!/usr/bin/env bash
# 5-agent Arc hunt cluster — Arc mainnet · real wallet USDC · bet cap
# Agents: SCANNER / NARRATIVE / RISK(AUDIT) / TIMING / EXIT
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PATH="${ROOT}:${PATH}"
export PYTHONPATH="${ROOT}/grok-trading-desk/.vendor:${ROOT}:${PYTHONPATH:-}"

# Load .env first so ARC_PRIVATE_KEY / RPC overrides apply
if [[ -f "${ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/.env" 2>/dev/null || true
  set +a
fi

# Mainnet defaults. A copied start must not fall back to testnet if .env misses a line.
export ARC_RPC_URL="${ARC_RPC_URL:-https://rpc.mainnet.arc.io}"
export ARC_BACKUP_RPC_URL="${ARC_BACKUP_RPC_URL:-https://rpc.drpc.mainnet.arc.io}"
export ARC_CHAIN_ID="${ARC_CHAIN_ID:-5042}"
export ARC_NETWORK="${ARC_NETWORK:-mainnet}"
export ARC_SYMBOL="${ARC_SYMBOL:-USDC}"
export DESK_CHAIN=arc
export DESK_FASTFORWARD=0
export DESK_WS_ENABLED="${DESK_WS_ENABLED:-0}"
export DESK_PAPER=0
export MODE=live

# Real balance (no 1000 USDC fake floor)
export ARC_WALLET="${ARC_WALLET:-}"
export ARC_FAKE_WHEN_ZERO=0
unset ARC_FAKE_BALANCE_USDC 2>/dev/null || true

# Single bet stays inside the 1 USDC live ceiling
export ARC_BET_PCT="${ARC_BET_PCT:-0.10}"
# DESK_STAKE left unset → UI uses on-chain balance

# Hunt lock only if real Warp CAs are already in the environment.
# Do not inject the old placeholder addresses.
export ARC_BET_HARD_CAP="${ARC_BET_HARD_CAP:-1}"
export DESK_ENTRY="${DESK_ENTRY:-0.50}"

# Unknown audit fields stay REVIEW. Do not default back to stub.
export ARC_AUDIT_MODE="${ARC_AUDIT_MODE:-live}"
export ARC_AUDIT_GOPLUS="${ARC_AUDIT_GOPLUS:-0}"
export ARC_AUDIT_DEX="${ARC_AUDIT_DEX:-1}"

# Narrative LLM (OpenRouter via ANTHROPIC_* in .env) + token-fee gate
export AGENT_LLM="${AGENT_LLM:-1}"
export LLM_RESERVE_USDC="${LLM_RESERVE_USDC:-100}"
export LLM_COST_PER_CALL_USDC="${LLM_COST_PER_CALL_USDC:-0.03}"
export LLM_REQUIRE_EARNINGS="${LLM_REQUIRE_EARNINGS:-1}"

# Broadcast still opt-in (./scripts/run_5_bots_cluster.sh --live)
export ARC_FORCE_BROADCAST="${ARC_FORCE_BROADCAST:-0}"
export ARC_SKIP_BALANCE_CHECK="${ARC_SKIP_BALANCE_CHECK:-0}"

PORT="${PORT:-8501}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --live) export ARC_FORCE_BROADCAST=1; export ARC_LIVE=1; shift ;;
    *) shift ;;
  esac
done

echo "═══════════════════════════════════════════════════"
echo "  miki · 5-bots Arc desk"
echo "  RPC  ${ARC_RPC_URL}"
echo "  backup ${ARC_BACKUP_RPC_URL}"
echo "  Chain ${ARC_CHAIN_ID} · ${ARC_SYMBOL}"
echo "  wallet ${ARC_WALLET}"
echo "  pads ${ARC_PADS:-warp}"
echo "  bet cap min(${ARC_BET_PCT}×bal, ${ARC_BET_HARD_CAP} USDC)"
echo "  force broadcast ${ARC_FORCE_BROADCAST}"
echo "  http://127.0.0.1:${PORT}"
echo "═══════════════════════════════════════════════════"

# Show live balance (non-fatal)
python3 - <<'PY' 2>/dev/null || true
from desk_realtime.arc_net import fetch_wallet_usdc
from desk_realtime.arc_hunt import capped_entry_usdc
info = fetch_wallet_usdc(force=True)
raw = float(info.get("raw_usdc") or 0)
print(f"  on-chain {raw:.4f} USDC · entry cap {capped_entry_usdc(raw):.4f}")
if info.get("faked"):
    print("  WARN: fake floor still active — check ARC_FAKE_WHEN_ZERO")
PY

exec python3 -m streamlit run miki_crypto_app.py \
  --server.port "${PORT}" \
  --server.headless true \
  --browser.gatherUsageStats false
