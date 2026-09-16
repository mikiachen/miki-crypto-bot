# miki crypto bot

Arc mainnet desk. Five robots scan Warp curves, score narrative, check risk, and only then broadcast a small USDC buy. The page is Streamlit. The loop is a separate process.

This is live software. A filled order spends real USDC. The public tree does not include a wallet, a private key, or API keys.

## Do not publish

Keep these on your machine only:

- `.env` — private key, wallet, OpenRouter key, X bearer token
- `grok-trading-desk/logs/` — session tape
- `grok-trading-desk/config.yaml`
- Foundry binaries (`cast`, `forge`, `anvil`) and `.video_frames/`

Copy `.env.mainnet.example` to `.env` and fill the blanks. The hard bet ceiling in the example is 1 USDC.

## Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-desk.txt
cp .env.mainnet.example .env
# paste ARC_PRIVATE_KEY, ARC_WALLET, and any LLM / X keys into .env

./scripts/run_5_bots_cluster.sh
# separate process, only when you mean to spend:
./scripts/supervise_arc_loop.sh --live --stake 0.5
```

Page: http://127.0.0.1:8501

Official HTTPS only. `http://niorfun.com` is not an RPC. The explorer `https://arc-scan.org` is not an RPC.

## What the robots do

| Robot | Job |
| --- | --- |
| SCANNER | Warp curves you listed in `ARC_WARP_CAS` |
| NARRATIVE | X search plus an LLM score. Below 0.62, or `off_narrative`, no buy |
| RISK | Honeypot if a provider says so. Same-wallet cluster can veto |
| TIMING | At least 3 external buyers, and `quoteBuy` / `quoteSell` both return |
| EXIT | After a fill, mark with `quoteSell` and sell |

The valve, the one-position rule, and the 1 USDC cap sit outside the robots. Dexscreener is read-only. A listed pair is not a buy. Tolly and DYOR stay dark until a verified mainnet swap ABI exists. Do not paste invented contract addresses.

GoPlus does not support Arc chain 5042. An unsupported-chain response is not a pass and not a fake honeypot. A real honeypot flag still stops the buy.

## Research lab

The older research engine lives in `ai_quant_lab/`. Notes: [docs/AI_QUANT_LAB.md](docs/AI_QUANT_LAB.md). License: MIT.
