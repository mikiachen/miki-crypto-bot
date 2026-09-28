# Robinhood desk · stock-token swing

Robinhood Chain mainnet (chain 4663). The loop trades tokenized US stocks against USDG on Uniswap v3, only while the US market is open, and only when the on-chain price tracks the real share price. The page is Streamlit. The loop is a separate process.

This is live software. A filled order spends real USDG, and every swap costs ETH gas.

## Strategy

| Item | Setting |
| --- | --- |
| Market | `liquid` universe: MSTR, COIN, NVDA, TSLA stock tokens, pools with at least 50k USDG liquidity |
| Cash | USDG |
| Hours | US session only: pre-open break, regular session, power hour |
| Stake | 1.50 USDG per buy, 2 open positions max, book at most 3.0 USDG |
| Day loss stop | 10 USDG |
| Scan / exit check | every 8 s / every 3 s |

Entry needs all of these:

- 5-minute momentum near a breakout of the recent high
- underlying daily trend not broken (soft equity TA check)
- the Robinhood quote says the stock is tradable and not halted
- on-chain price no more than 4% above the real share price
- a quoted buy-then-sell roundtrip keeps at least 97% of the stake
- 30 minutes since the last trade in the same symbol

Exit on the first of:

| Rule | Trigger |
| --- | --- |
| HARD STOP | mark at 0.97× entry |
| TAKE PROFIT | mark at 1.06× entry |
| RSI TAKE | RSI at or above 70 |
| EMA STOP | 1.5% below EMA55 |
| MAX HOLD | 6 hours in the trade |

Slippage is 1% on buys and 5% on sells so exits fill. When ETH for gas runs low, the loop swaps a little USDG for ETH.

THE BALANCE on the page is NAV: wallet USDG plus the live mark of open positions, one line.

## Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-desk.txt
cp .env.rh.example .env.rh
# paste RH_PRIVATE_KEY and RH_WALLET into .env.rh; fund the wallet with USDG and a little ETH

python3 scripts/rh_preflight.py
./scripts/run_rh_desk_ui.sh
```

Page: http://127.0.0.1:8501

`run_rh_desk_ui.sh` starts the page and `supervise_rh_loop.sh`. The loop reads the market but does not sign until `RH_UNI_SEND=1` is set in `.env.rh` (or the loop is started with `--live`). The approval gate starts closed; open it on the page.

Only one desk runs at a time. Starting the Robinhood loop stops the Arc loop.
