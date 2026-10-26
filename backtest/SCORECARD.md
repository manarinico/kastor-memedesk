# memedesk backtest scorecard (paper only), built Sun Oct 25 2026, ~3:30 PM ET

**Bottom line:** over this window and on this data, **neither book makes money**. The gated desk turns $150 into **$121.49 (−19.0%)**. The no-filter baseline turns $150 into **$87.19 (−41.9%)**. Before costs the desk is roughly flat (−$2.15 on 184 trades). Fees and slippage ($26.41, of which **$18.40 is the $0.05 network fee**) account for nearly the whole loss.

## Data window and coverage
* **Decision times:** every 20 min from **Sun Oct 11 4:20 PM ET to Sun Oct 25 3:20 PM ET** (14 days). **Dev** is Oct 11 4:20 PM to Oct 21 10:40 AM ET (first 70%). **Held-out** is Oct 21 10:40 AM to Oct 25 3:20 PM ET (last 30%). Each period starts a fresh book.
* **Prices:** GeckoTerminal 5-minute OHLCV in USD covering each pool's first ~83 h (1,000 candles). Decisions only use candles that had closed before the decision time.
* **Pool counts:**
  * **1,039** candidate pools created in the last 14 days, found on GeckoTerminal's free lists (top-volume and top-tx pages, trending 24h/6h, new_pools, per-DEX pages for pumpswap, raydium, raydium-launchlab, raydium-clmm, meteora, meteora-damm-v2).
  * **167** were skipped as too young. They were under 90 min old when fetched, so they couldn't produce a signal yet.
  * **872** were fetched. Every one returned candles (**0 empty responses**).
  * **33** were excluded as suspected bad prints (any single 5-min close more than 10x the previous close; see "raw" below).
  * **839** pools are in the backtest, covering **765** tokens.
* **Coverage is lopsided:** 672 of the 872 fetched pools were created Oct 24–25, and only about 200 are from Oct 11–23. The dev period therefore sees far fewer pools (60 desk trades in ~10 days) than the held-out period (129 trades in ~4 days).
* **Scout (current RugCheck state applied backwards):** of 765 tokens, **301 pass all safety gates**. Blocks: LP not ≥95% locked/burned 317, top-10 holders ≥30% 134, mint authority set 11, freeze authority set 2. RugCheck answered for every token. A missing report would have counted as a skip.

## Scorecard: $150 book (the real case)
| Run | Start → End | Net P&L $ (%) | Trades | Win rate | Avg win / avg loss $ | Max DD | Total costs $ (DEX fee / network fee / slippage) | P&L before costs $ | Best trade | Worst trade |
|---|---|---|---|---|---|---|---|---|---|---|
| Desk (gated) – full window | $150 → $121.49 | -28.51 (-19.0%) | 184 | 25% | +1.02 / -0.55 | 21.0% | 26.41 (2.99 / 18.40 / 5.02) | -2.15 | BLACKTOBER +5.60 (+207%, take_profit) | NODF -2.65 (-102%, liquidity_drop) |
| Desk (gated) – dev (first 70%) | $150 → $134.26 | -15.74 (-10.5%) | 60 | 15% | +1.18 / -0.52 | 12.7% | 8.68 (1.00 / 6.00 / 1.68) | -7.25 | VEIL +3.86 (+129%, take_profit) | VEIL -0.99 (-32%, stop_loss) |
| Desk (gated) – held-out (last 30%) | $150 → $134.35 | -15.65 (-10.4%) | 129 | 30% | +1.13 / -0.64 | 15.8% | 19.13 (2.32 / 12.90 / 3.91) | +3.56 | BLACKTOBER +6.35 (+207%, take_profit) | MI -3.02 (-101%, liquidity_drop) |
| Baseline (no gates) – full window | $150 → $87.19 | -62.81 (-41.9%) | 402 | 23% | +0.87 / -0.47 | 43.5% | 54.91 (5.49 / 40.20 / 9.22) | -8.10 | Kimchi +7.05 (+314%, take_profit) | GTA 6 -1.91 (-102%, stop_loss) |
| Baseline (no gates) – dev (first 70%) | $150 → $115.26 | -34.74 (-23.2%) | 168 | 19% | +1.02 / -0.49 | 27.1% | 23.75 (2.59 / 16.80 / 4.36) | -11.29 | Kimchi +7.05 (+314%, take_profit) | ETCHY -1.68 (-55%, stop_loss) |
| Baseline (no gates) – held-out (last 30%) | $150 → $122.21 | -27.79 (-18.5%) | 230 | 28% | +1.08 / -0.58 | 21.3% | 33.44 (3.88 / 23.00 / 6.56) | +5.80 | Moustache +8.04 (+274%, take_profit) | GTA 6 -2.63 (-102%, stop_loss) |

## Sensitivity: same rules on a $1,000 book
| Run | Start → End | Net P&L $ (%) | Trades | Win rate | Avg win / avg loss $ | Max DD | Total costs $ (DEX fee / network fee / slippage) | P&L before costs $ | Best trade | Worst trade |
|---|---|---|---|---|---|---|---|---|---|---|
| Desk (gated) – full window | $1,000 → $877.25 | -122.75 (-12.3%) | 186 | 24% | +7.95 / -3.41 | 14.5% | 79.68 (21.22 / 18.60 / 39.86) | -43.50 | BLACKTOBER +40.90 (+213%, take_profit) | MI -19.11 (-100%, liquidity_drop) |
| Desk (gated) – dev (first 70%) | $1,000 → $925.63 | -74.37 (-7.4%) | 60 | 15% | +8.65 / -2.98 | 9.8% | 25.28 (6.84 / 6.00 / 12.44) | -49.62 | VEIL +26.54 (+133%, take_profit) | VEIL -6.14 (-30%, stop_loss) |
| Desk (gated) – held-out (last 30%) | $1,000 → $956.17 | -43.83 (-4.4%) | 130 | 29% | +8.36 / -3.93 | 12.7% | 60.37 (16.17 / 13.00 / 31.20) | +16.77 | BLACKTOBER +44.57 (+213%, take_profit) | MI -20.82 (-100%, liquidity_drop) |
| Baseline (no gates) – full window | $1,000 → $779.96 | -220.04 (-22.0%) | 402 | 25% | +6.97 / -3.04 | 26.1% | 162.46 (41.82 / 40.20 / 80.44) | -58.30 | Kimchi +53.26 (+324%, take_profit) | GTA 6 -16.12 (-100%, stop_loss) |
| Baseline (no gates) – dev (first 70%) | $1,000 → $852.37 | -147.63 (-14.8%) | 168 | 20% | +7.57 / -2.94 | 20.3% | 70.12 (18.21 / 16.80 / 35.11) | -78.48 | Kimchi +53.26 (+324%, take_profit) | ETCHY -10.85 (-53%, stop_loss) |
| Baseline (no gates) – held-out (last 30%) | $1,000 → $930.06 | -69.94 (-7.0%) | 230 | 29% | +7.97 / -3.70 | 17.1% | 105.18 (27.66 / 23.00 / 54.52) | +35.66 | Moustache +57.92 (+279%, take_profit) | GTA 6 -19.20 (-100%, stop_loss) |

**Spy (wallet copy): not backtestable from free data.** GeckoTerminal's free `/trades` endpoint only returns the last ~300 trades / 24 h per pool. Historical wallet P&L needs Birdeye (key), GMGN (Cloudflare) or heavy RPC (429s). No Spy numbers are reported.

**Circuit breakers (desk only):** the 4-losses-in-a-row breaker blocked entries on **410 of ~1,000 decision cycles** ($150 full run). The 5% daily-loss breaker blocked 40 cycles. Liquidity-drop exits happened 2 times. Stale data does not apply in a backtest.

### Cost drag in plain numbers
* **At $150:** a position is ~$3. Each round trip costs $0.10 network fee (3.3%), ~0.6% DEX fee and ~1% slippage, so about **5% round trip**. The desk paid $26.41 in costs on 184 trades (17.6% of the starting book). The network fee alone was **$18.40 (12.3% of the book)**.
* **At $1,000:** a position is ~$20 and a round trip costs about 2.2%. The desk paid $79.68 (8.0% of the book).
* **Edge before costs is about zero:** the desk averages −0.4% per trade before costs over the full window, −4% in dev and +0.9% in held-out. Win rate is 25%, against an average win of about 2x the average loss. That needs ~33% to break even before costs.

### Raw-data run (bad prints kept), for transparency (`results_raw.json`, `trades_raw.csv`)
| $150 book | Full window | Dev | Held-out |
|---|---|---|---|
| Desk | $120.71 (−19.5%, 190 trades) | $133.95 | $134.30 |
| Baseline | **$245.96 (+64%, 431 trades)** | $112.20 | $356.18 |

In this run the baseline's whole "profit" is **one trade**: SWORDOPE (pool EjRDzQkK…) bought Oct 24 9:40 PM ET and sold at the 10:20 PM ET check for **+10,324% (+$173.68)**. The price jumps 85x in a single 5-minute candle, and that candle is the last candle GeckoTerminal has for the pool. Without that trade the raw baseline is about −$78. It looks like a bad print, not a tradable move, which is why the headline tables exclude the 33 pools with such jumps.

## How the rules were simulated
* **Rule constants, fills and gates come from the live package, read-only:**
  * `memedesk.config`, `memedesk.fills.buy/sell` (0.30% fee, $0.05 network fee per fill, 0.5% slippage plus impact = size ÷ (liquidity ÷ 2))
  * `memedesk.scout.safety_gates`
  * `memedesk.risk` breakers
* **Entries:**
  * 1h change: last close vs the close 1 h earlier
  * 5m change: last close vs the previous close
  * 1h volume: sum of the last 12 candles
  * Buy/sell ratio: **proxy** = volume of green candles ÷ volume of red candles over the last hour. Gecko OHLCV has no trade counts.
  * Size = 2% of current equity; at most 5 open; 2-hour re-entry cooldown per token. Candidates are ranked by 1h volume.
* **Exits, checked every 20 min:**
  * Stop-loss triggers if any candle low since the last check is at or below −12%. It fills at the **worse** of the stop price and the current close.
  * Take-profit only when the close at the check is at or above +25%, filled at that close. Highs between checks are not used.
  * 4-hour time stop.
  * Every exit then goes through `fills.sell`, so fees and slippage are taken off the exit price.
  * Open positions are marked at liquidation value for equity and drawdown.
* **Baseline:** same signal and exits, no Scout gates (no age, authority, LP or holder gates) and no breakers. It keeps only the live baseline's $5k liquidity floor.

## Limitations (what the data can't support)
1. **Survivorship.** Pools that rugged or died before today don't appear on today's lists, so they never entered the universe. "0 empty OHLCV responses" means the lists only show pools that still exist. It is not evidence that the bias is absent. Some dead pools are still in: 423 of 872 now have under $1k liquidity.
2. **Scout safety state is today's, not historical.** A token that revoked authority or locked LP later is treated as safe all along, and the reverse.
3. **Liquidity history is estimated.** It is today's reserve × √(price then ÷ price now), a constant-product approximation that is wrong for CLMM/DLMM pools and LP pulls. The $20k gate, price impact and the 40% liquidity-drop breaker all rely on it. The −100% "liquidity_drop" trades come from this estimate collapsing.
4. **The buy/sell ratio is a candle proxy, not real buy and sell counts.**
5. **Sample:** 184 desk trades, but most come from Oct 22–25 pools. Dev and held-out are not comparable in coverage. One or two big take-profits move the totals by several dollars.
6. **Fills:** 3 s latency and MEV aren't modeled. The conservative stop-loss fill (worse of stop and close) makes stops expensive (several −30% to −50% "stop" exits).

## Side note: the live desk's own replay (today only, $1,000 book)
`replay --dev-frac 0.7` was run on a **read-only copy** of today's snapshots (output in `replay_side/replay_output.txt`).
* Dev covered 10 cycles, 10:11–10:50 AM ET: desk −0.42%, baseline −0.95%.
* Eval covered 5 cycles, 10:52 AM–2:24 PM ET: desk −0.75%, baseline −2.40%.
* That is 3–4 closed trades per side, so it's noise, as the report itself says.

## Files
* `trades.csv` (headline, cleaned) and `trades_raw.csv` contain every simulated trade, tagged by run.
* `results.json` and `results_raw.json` hold the summary numbers.
* `candidates.json` lists the pool universe.
* `cache/` holds every API response.
* Code: `backtest.py`, `run_all.py`, `discover.py`, `fetch_data.py`, `make_scorecard.py`.
