# ❄ NORWAY — pump.fun graduate scanner, decision engine and paper trader (Solana)

Paper trader + dashboard + backtester + optimizer + research collector for a Raspberry Pi (64-bit).
The backtester and the paper trader run the **same** strategy functions (`norway_strategy.py`) and the **same**
account book (`norway_core.Book`), so a paper trade and a backtest trade come from the same code.

## Read this first: the research did not find a profitable strategy

The goal was a strategy that is "very profitable" on pump.fun coins, with proof. I could not produce that, and
this repository does not claim it.

**What was tested.** An unbiased sample of pump.fun graduations (tokens that migrated to PumpSwap), read straight
from the chain so that every token that died afterwards is included: **412 tokens, 14 Sep → 13 Nov 2026,
395,000 five-minute candles**, drawn at random from about 125,000 graduations in that period.

**What the sample looks like**

| | |
|---|---|
| Tokens below half of their graduation price at the end of their data | **99.0 %** (median: 6 % of the graduation price) |
| Market cap under $50k one hour after graduation (graduation ≈ $33k) | 82.5 % |
| Pushed above 3× within the first 10 minutes (one wallet buying the pool) | 40.5 % (25.5 % above 10×) |
| Had a single 5-minute candle that fell ≥ 70 % from a market cap ≥ $100k | 45.4 % |
| Above their graduation price six hours later | 29 tokens, only 13 of them without a bundle pump |

**What the strategies did.** 3,000 random parameter sets of the breakout / momentum family (ages from minutes to a
week, market caps $30k–$100M, stops 10–40 %, trailing, take-profit, time stops, with and without the manipulation
filters), each run on the first and on the second half of the period separately:

| Fill model | Sets with ≥ 8 trades per half | Profitable in both halves | Median result per trade |
|---|---|---|---|
| Realistic (a rug candle fills the stop at the candle's close) | 162 | **0** | **−1.5 R / −1.6 R** |
| Optimistic (every stop fills at the stop price) | 162 | 7 (4 %, what chance produces) | −0.5 R / −0.4 R |

Reproduce: `python research/random_search.py 3000` and `CRASH=0 python research/random_search.py 3000`.

Also negative: buying sharp dips in surviving tokens (54 variants, none positive in both halves), and the plain
forward drift of tokens that are still alive (−6 % to −25 % over the next six hours in every age / size bucket
with enough data).

**The shipped default** ("organic survivor breakout", below) made **5 trades on 1 token: −4.4 R, $100 → $91.56**
in the backtest; the walk-forward check gives the same −4.4 R. It is the most selective variant, not a proven one.
Organic survivors are about 3 % of graduates, so this sample holds too few of them to judge that idea either way.

**Why.** A round trip costs 2.5–3.5 % (PumpSwap charges 1.0–1.25 % per swap at these market caps), almost every
token goes to near zero, and the exits that matter happen inside one transaction, where no stop can fill.

**What this is good for, then**
* A **forward test at no cost**: the paper trader sees *every* graduation live (about 2,500 a day, the backtest
  sample is 0.3 % of them) and applies the full fee model and the live safety gate.
* A **decision engine**: in APPROVE mode it only alerts you (Discord + dashboard) with the complete safety report
  and waits for your click.
* A **research bench**: the collector, backtester and optimizer are there to test other ideas on honest data.
  Run the collector on the Pi for a few days to get a sample large enough to judge the survivor idea.

Real-money execution is not implemented and stays locked in code. On this evidence it should stay that way.

## The default strategy
1. **Universe** – every pump.fun graduation with ≥ 50 SOL seeded into the PumpSwap pool. In this universe the
   launchpad itself revokes mint + freeze authority and burns the LP, so the classic honeypot is not possible;
   the real risks are bundles, insiders and dumps.
2. **Entry** (5-minute candles, all eight must be true at a candle close; buy at the next price):
   age 8 h – 7 d · market cap $150k – $5M · close above the highest high of the last 24 candles · ≥ $10k volume in
   the last hour · ≥ 75 % of those candles traded · not more than 2.5× above the range low ·
   hourly volume ≥ 2 % of the market cap · price was **not** pushed above 3× the graduation price in the first 10 minutes.
3. **Exit** – hard stop −25 %, trailing stop 25 % under the highest close, time stop 48 h, optional partial take-profit.
4. **Size** – 2 % of the account at risk per trade (position = risk ÷ stop distance), at most 20 % of the account,
   at most 1 % of the pool's SOL side, at most 4 positions, no new entries after −10 % on the day.
5. **Safety gate** (live only) – before every buy: RugCheck (authorities, transfer tax, largest wallet, top-10
   wallets, insider networks, holders, danger flags), DexScreener (liquidity, trades and sell pressure of the last
   hour) and a Jupiter buy + sell quote of the real order size (detects taxes and blocked sells). No data = no trade.

Every parameter is editable on the dashboard (CONTROL) and by the optimizer.

## Venue and fees
There is no exchange with a demo account for these coins. The realistic venue is your own Solana wallet trading
through the **Jupiter** aggregator into the token's **PumpSwap** pool. Applied to every paper and backtest fill:

| Cost | Value |
|---|---|
| PumpSwap fee per swap | 1.25 % below 420 SOL market cap, falling in 25 tiers to 0.30 % above 98,240 SOL |
| Jupiter | 10 bps |
| Slippage | 0.5 % per fill, +2 % when a stop is hit |
| Price impact | order size ÷ SOL side of the pool |
| Network | $0.03 per transaction (base + priority fee + tip) |

The tier table is taken from secondary sources that quote pump.fun's documentation; check it against the official
fee page before relying on it (`FEE_TIERS` in `norway_strategy.py`).

**DEMO** on the dashboard is a *shadow executor*: every paper fill is priced with a real Jupiter quote of the same
size and the difference is recorded. That is the test of the fee and slippage model.

## Files
| File | Purpose |
|---|---|
| `norway_strategy.py` | parameters, fee tiers, indicators, entry signal, stop / trail / take-profit logic |
| `norway_core.py` | config, logging + Discord, data clients, safety gate, account book, statistics, journal |
| `norway_trader.py` | live paper trader — tmux session **Norway** |
| `norway_web.py` | dashboard + API on port **8759** — tmux session **NorwayWeb** |
| `norway_backtest.py` | backtester, optimizer, walk-forward (website and command line) |
| `norway_exec.py` | shadow executor (demo), key storage, the locked live cockpit |
| `static/index.html` | the dashboard (vault lock screen, live, backtest, optimizer, control) |
| `research/collect.py` | builds the unbiased dataset from the chain + GeckoTerminal |
| `research/dataset.jsonl.gz` | the 412-token research dataset |
| `research/random_search.py` | the robustness test quoted above |
| `tests/test_trader_offline.py` | offline check of the trader's buy → trail → stop → approval path |
| `setup.sh` / `start.sh` / `stop.sh` | install / start the 3 tmux sessions / stop |

## Setup on the Pi (Raspberry Pi OS Bookworm, 64-bit)
```bash
cd ~
git clone https://github.com/investorgative/Norway.git norway
cd norway
git checkout claude/upbeat-mendel-djovl9      # until this branch is merged into main
chmod +x setup.sh && ./setup.sh               # venv, packages, cloudflared, asks for the website passcode
./start.sh                                    # starts Norway, NorwayWeb, NorwayCloud
```
Dashboard on your network: `http://<pi-ip>:8759`. The public `trycloudflare.com` address is printed by `start.sh`
and is behind the **PUBLIC** chip in the header (click = copy). A free quick tunnel gets a new random address
whenever `NorwayCloud` restarts. No account and no API key is needed for anything.

### Discord
Dashboard → **CONTROL** → Discord → paste the webhooks → SAVE & TEST (or `nano config.json` → `"discord"`).
* `trades` – every buy, take-profit, stop raise and sell. The bot trades many different coins, so this is one
  channel for all of them; each message carries the coin and its DexScreener link.
* `alerts` – approval requests and blocked signals (optional; falls back to `trades`).
* `errors` – every ERROR from trader, web server, backtester and optimizer (same message at most once per 5 min).

Discord: Server settings → Integrations → Webhooks → New webhook → copy URL. One per channel.

### Daily use
```bash
tmux attach -t Norway         # paper trader      (leave with Ctrl+B, then D)
tmux attach -t NorwayWeb      # web server
tmux attach -t NorwayCloud    # cloudflare tunnel (shows the public URL)
./stop.sh && ./start.sh       # restart everything
git pull && ./stop.sh && ./start.sh                                    # update
./stop.sh && ./venv/bin/python norway_trader.py --reset && ./start.sh  # wipe the paper account
```
Autostart after reboot: `crontab -e`, add `@reboot sleep 30 && /home/<user>/norway/start.sh`.

### Command line
```bash
./venv/bin/python norway_backtest.py                          # default parameters, whole dataset
./venv/bin/python norway_backtest.py --days 30 --risk 2 --balance 100 --set stop_pct=30
./venv/bin/python norway_backtest.py --optimize --combos 300
./venv/bin/python norway_backtest.py --walk
./venv/bin/python tests/test_trader_offline.py
```

### A bigger dataset (recommended before trusting anything)
```bash
tmux new -s NorwayData "./venv/bin/python research/collect.py --days 60 --per-day 300"
```
Resumable; results go to `research/data.db` and the backtester picks them up automatically. The public Solana RPC
allows about 20 transactions a minute, GeckoTerminal 10–30 candle downloads. With a free CoinGecko Demo key
(`--cg-key YOUR_KEY`) the candle side runs at a steady 30 a minute. Stop the paper trader while collecting, or give
one of them its own RPC (CONTROL → Demo trading → Solana RPC URL), because both use the same public rate limit.

## The website
* **Vault** – every visit and reload starts at the lock screen. The passcode (4–8 digits) is checked on the Pi; the
  page holds no data and every API call needs a token only a correct code hands out. From the 6th wrong code the
  wait escalates (1, 5, 15, 60 min). Auto-lock after 10 idle minutes (`web.autolock_minutes`). Change the code:
  `nano config.json` → `"web": {"passcode": "..."}` → restart.
* **Header** – focus coin with price and market cap, 5-minute candle countdown, SOL price, clock and date,
  PAPER / DEMO / LIVE switch, bot heartbeat, public address, lock.
* **LIVE** – account overview with every statistic, equity curve ($ or R), live chart with the breakout level,
  entries, exits and stop, strategy status (funnel from graduations seen to positions, what the bot waits for, the
  eight conditions per candidate), approval cards, open positions, event feed, journal (year → month → week → day,
  default: 4 weeks), trade log (click a row for its chart), log.
* **BACKTEST** – period, risk, balance and parameters; the result is shown in the same interface.
* **OPTIMIZER** – random search with a train / test split in time; every row can be backtested or applied.
* **CONTROL** – AUTO / APPROVE, account and risk, strategy parameters, safety gate, Discord, demo keys with the
  shadow-fill table, live wallet and keys (locked).

### Demo and live keys
* **Demo:** both fields are optional. A free Jupiter key (portal.jup.ag) and a free RPC URL (e.g. Helius) only
  raise the rate limits. Switch "shadow every paper fill" on to start recording quotes.
* **Live:** the header switch, the wallet / key storage and the connection test are built. Execution is switched
  off in code (`LIVE_ENABLED = False` in `norway_exec.py`); nothing in the repository can sign a transaction.
* Keys are stored in `data/secrets.json` (chmod 600), never sent back to the browser and never in git.

## Known limits
* The safety gate cannot be backtested (no historical holder data), so the backtest is the strategy *without* it.
* The backtest sample is 0.3 % of all graduations: it measures the result per trade, not how often the live bot trades.
* Live candles are built from price polls every 15 s and confirmed against GeckoTerminal's exchange candles before
  an entry; the backtest uses the exchange candles throughout.
* Tokens added by the start-up bootstrap (already trading when the bot starts) use a standard graduation price for
  the bundle filter, because their migration transaction was not seen.
* Nothing here is financial advice. Past results, including negative ones, do not predict anything.

Chart library: TradingView Lightweight Charts 4.1.3 (Apache-2.0), bundled in `static/`.
