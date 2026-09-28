# miki crypto bot

One Streamlit trading desk, two chains, two strategies. Pick the one you want to run.

| | Arc desk | Robinhood desk |
| --- | --- | --- |
| Chain | Arc mainnet (5042) | Robinhood Chain (4663) |
| Trades | New meme tokens on Warp bonding curves | Tokenized US stocks (MSTR, COIN, NVDA, TSLA) on Uniswap v3 |
| Cash | USDC | USDG |
| Hours | 24/7 | US market session only |
| Signal | X narrative + LLM score, buyer count, honeypot check | Price momentum, daily trend, real-share premium, halt check |
| Exit | Stop 0.85×, trailing from 2×, take profit 8×, 45 min max | Stop 0.97×, take profit 1.06×, RSI / EMA exit, 6 h max |
| Risk | High: few big winners, many small losers | Lower: small moves, small stakes |
| Config | `.env.mainnet.example` → `.env` | `.env.rh.example` → `.env.rh` |
| Start | `./scripts/run_5_bots_cluster.sh` | `./scripts/run_rh_desk_ui.sh` |
| Guide | [docs/ARC.md](docs/ARC.md) | [docs/ROBINHOOD.md](docs/ROBINHOOD.md) |

Only one desk runs at a time. Both use the same page at http://127.0.0.1:8501.

This is live software. A filled order spends real money. The public tree does not include a wallet, a private key, or API keys.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-desk.txt
```

Then follow the guide for the desk you picked.

## Do not publish

Keep these on your machine only:

- `.env`, `.env.rh` — private keys, wallets, OpenRouter key, X bearer token
- `grok-trading-desk/logs/` — session tape and trade ledger
- `grok-trading-desk/config.yaml`
- Foundry binaries (`cast`, `forge`, `anvil`) and `.video_frames/`
