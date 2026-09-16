#!/usr/bin/env bash
# Prepend project-root Foundry binaries; Arc TESTNET defaults.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="${ROOT}:${PATH}"
export ARC_RPC_URL="${ARC_RPC_URL:-https://rpc.testnet.arc.io}"
export ARC_BACKUP_RPC_URL="${ARC_BACKUP_RPC_URL:-https://rpc.testnet.arc.io}"
export ARC_CHAIN_ID="${ARC_CHAIN_ID:-5042002}"
export ARC_SYMBOL="${ARC_SYMBOL:-USDC}"
echo "Foundry PATH → ${ROOT}"
echo "  ARC_RPC_URL=${ARC_RPC_URL}  backup=${ARC_BACKUP_RPC_URL}  chain=${ARC_CHAIN_ID}"
command -v cast >/dev/null && cast --version | head -1 || echo "  WARN: cast not executable"
command -v forge >/dev/null && forge --version | head -1 || echo "  WARN: forge not executable"
