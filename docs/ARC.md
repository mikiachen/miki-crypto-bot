# Arc desk · meme-launch hunter

Arc mainnet (chain 5042). Five robots scan Warp bonding curves, score narrative, check risk, and only then broadcast a small USDC buy. The page is Streamlit. The loop is a separate process.

This is live software. A filled order spends real USDC.

## Strategy

| Item | Setting |
| --- | --- |
| Market | New tokens on Warp curves you list in `ARC_WARP_CAS` / `ARC_WARP_FACTORY` |
| Cash | USDC |
| Hours | 24/7 |
| Stake | Hard cap 1 USDC per buy (`ARC_BET_HARD_CAP`); `--stake 0.5` for trials |
| Slots | 2 open positions max, day loss stop 10 USDC |
| Exit | Hard stop 0.85×, trailing stop armed at 2× with 20% giveback, take profit 8×, max hold 45 min |

## Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-desk.txt
cp .env.mainnet.example .env
# paste ARC_PRIVATE_KEY, ARC_WALLET, and any LLM / X keys into .env

python3 scripts/arc_preflight.py --mainnet
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
