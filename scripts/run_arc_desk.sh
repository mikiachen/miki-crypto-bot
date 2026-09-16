#!/usr/bin/env bash
# Arc mainnet desk — local forge/cast on PATH + Streamlit UI
# Official RPC: https://rpc.mainnet.arc.io  (Chain ID 5042)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Foundry binaries dragged into project root
export PATH="${ROOT}:${PATH}"
export PYTHONPATH="${ROOT}/grok-trading-desk/.vendor:${ROOT}:${PYTHONPATH:-}"

# Load private key + overrides from .env (never print the key)
if [[ -f "${ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/.env" 2>/dev/null || true
  set +a
fi

export ARC_RPC_URL="${ARC_RPC_URL:-https://rpc.mainnet.arc.io}"
export ARC_BACKUP_RPC_URL="${ARC_BACKUP_RPC_URL:-https://rpc.drpc.mainnet.arc.io}"
export ARC_CHAIN_ID="${ARC_CHAIN_ID:-5042}"
export ARC_SYMBOL="${ARC_SYMBOL:-USDC}"
# Warp-only until Tolly/DYOR mainnet ABIs verified
export ARC_PADS="${ARC_PADS:-warp}"
export ARC_ALLOW_PLACEHOLDERS="${ARC_ALLOW_PLACEHOLDERS:-0}"
export ARC_EXPLORER="${ARC_EXPLORER:-https://arc-scan.org}"
export ARC_NETWORK="${ARC_NETWORK:-mainnet}"

# Desk: live Arc block in topbar (not fast-forward tape)
export DESK_FASTFORWARD="${DESK_FASTFORWARD:-0}"
export DESK_WS_ENABLED="${DESK_WS_ENABLED:-0}"
export DESK_PAPER=0
export MODE=live
# Arc three-tier audit (拒绝 / 待复核 / 可看) — unknown = REVIEW, never auto-buy
export ARC_AUDIT_MODE="${ARC_AUDIT_MODE:-live}"
export ARC_AUDIT_GOPLUS="${ARC_AUDIT_GOPLUS:-0}"
export ARC_AUDIT_DEX="${ARC_AUDIT_DEX:-1}"
# Arc book — prefer live wallet; no default 1000 fake stake
export DESK_ENTRY="${DESK_ENTRY:-0.50}"
export ARC_BET_HARD_CAP="${ARC_BET_HARD_CAP:-1}"
# Real wallet balance (fake floor OFF)
export ARC_FAKE_WHEN_ZERO="${ARC_FAKE_WHEN_ZERO:-0}"
# Real cast/web3 broadcast — set ARC_FORCE_BROADCAST=1 + ARC_PRIVATE_KEY in .env
export ARC_FORCE_BROADCAST="${ARC_FORCE_BROADCAST:-0}"
export ARC_SKIP_BALANCE_CHECK="${ARC_SKIP_BALANCE_CHECK:-0}"

PORT="${PORT:-8501}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --ff) export DESK_FASTFORWARD=1; shift ;;
    --live) export ARC_FORCE_BROADCAST=1; export ARC_LIVE=1; shift ;;
    *) shift ;;
  esac
done

echo "═══════════════════════════════════════════════════"
echo "  miki desk · Arc ${ARC_NETWORK:-TESTNET}"
echo "  RPC  ${ARC_RPC_URL}"
echo "  backup ${ARC_BACKUP_RPC_URL}"
echo "  Chain ${ARC_CHAIN_ID} · ${ARC_SYMBOL}"
echo "  pads ${ARC_PADS} · placeholders ${ARC_ALLOW_PLACEHOLDERS}"
# Never invoke ./cast under set -e — missing libusb → Abort trap can kill the shell
CAST_OK=0
if [[ -x "${ROOT}/cast" ]]; then
  if ( "${ROOT}/cast" --version >/dev/null 2>&1 ); then
    CAST_OK=1
    echo "  cast $(${ROOT}/cast --version 2>/dev/null | head -1 || echo ok)"
  else
    echo "  WARN: ./cast needs libusb — brew install libusb"
    echo "        (desk uses JSON-RPC / web3 fallback)"
  fi
else
  echo "  WARN: ./cast missing — will use JSON-RPC fallback"
fi
if [[ -x "${ROOT}/forge" ]]; then
  echo "  forge present"
else
  echo "  WARN: ./forge missing"
fi
if [[ -x "${ROOT}/arc" ]]; then
  echo "  arc present"
else
  echo "  note: ./arc binary optional (not required for block height)"
fi
echo "  wallet ${ARC_WALLET}"
echo "  bet cap min(${ARC_BET_PCT:-0.10}×bal, ${ARC_BET_HARD_CAP:-50} USDC)"
echo "  fake when zero ${ARC_FAKE_WHEN_ZERO}"
echo "  force broadcast ${ARC_FORCE_BROADCAST} · skip bal check ${ARC_SKIP_BALANCE_CHECK}"
if [[ -n "${ARC_PRIVATE_KEY:-}" ]]; then
  echo "  private key LOADED (from .env)"
else
  echo "  WARN: ARC_PRIVATE_KEY empty — fill .env before live sends"
fi
echo "  http://127.0.0.1:${PORT}"
echo "═══════════════════════════════════════════════════"

# Optional smoke (non-fatal, short timeout) — skip cast when broken
if [[ "${CAST_OK}" == "1" ]]; then
  BN="$(${ROOT}/cast block-number --rpc-url "${ARC_RPC_URL}" 2>/dev/null || true)"
  if [[ -z "${BN}" && -n "${ARC_BACKUP_RPC_URL:-}" ]]; then
    BN="$(${ROOT}/cast block-number --rpc-url "${ARC_BACKUP_RPC_URL}" 2>/dev/null || true)"
  fi
  [[ -n "${BN}" ]] && echo "  live block ${BN}"
else
  BN="$(python3 -c "
import json,urllib.request
for u in ('${ARC_RPC_URL}','${ARC_BACKUP_RPC_URL}'):
  try:
    req=urllib.request.Request(u,data=json.dumps({'jsonrpc':'2.0','id':1,'method':'eth_blockNumber','params':[]}).encode(),headers={'Content-Type':'application/json'})
    print(int(json.load(urllib.request.urlopen(req,timeout=5))['result'],16))
    break
  except Exception:
    pass
" 2>/dev/null || true)"
  [[ -n "${BN}" ]] && echo "  live block ${BN} (rpc)"
fi

exec python3 -m streamlit run miki_crypto_app.py \
  --server.address 127.0.0.1 \
  --server.port "${PORT}" \
  --server.headless true \
  --browser.gatherUsageStats false
