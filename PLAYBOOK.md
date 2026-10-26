# Kastor Trades Playbook

One section per lane. Every claim cites desk data. Ideas here are hypotheses until they pass replay, hold on live snapshots (30+ closed trades), clear Risk Desk, and get Manari approval.

## Trends and momentum (Gambler)

Source: `backtest/trades.csv`, run `desk_150_full` (184 trades, Oct 11 to 25), split 70/30 by entry time with the cut at 2026-10-24 01:20 ET. Survivor-biased, so these are ideas only (Risk Desk).

**Current rules:** entry needs a 1-hour change of +5 to +60%, a 5-minute change of at least 0, a buy/sell ratio of at least 1.2 and 1-hour volume of at least $10k. Exits are +25% take-profit, -12% stop and a 4-hour time stop.

**What the data says**
- No edge yet. The win rate is 25% against a breakeven of about 35% at the 1.85:1 win/loss size (Risk Desk check).
- Stops drive the losses. Stop-losses were 126 of the 184 trades for -$68.05. The 33 take-profits made +$43.87, the 23 time stops +$0.86 and the 2 liquidity-drop exits -$5.19.
- No single range of 1-hour change, 5-minute change, buy/sell ratio, 1-hour volume or pool age was profitable after costs in both dev and held-out.
- Ranges that looked good in dev and then failed in held-out:
  - 1-hour change of 20–30%: +$5.32 in dev, -$2.42 in held-out.
  - Buy/sell ratio of 2–3: +$3.56 in dev, -$1.42 in held-out.
  - 5-minute change above 6%: +$4.03 in dev, -$3.06 in held-out.
- The one consistent sign is a 5-minute change of 0 to 1%, meaning the coin is not still spiking at entry. It won 38% in dev (n=29) and 62% in held-out (n=13), with gross P&L of +$0.66 and +$0.60. It was still net negative after costs, and the sample is small.
- Cost drag is large at $3 positions, about 5% per round trip (Manari scorecard). Net P&L was -$28.51, of which $26.41 was costs.

**Hypotheses to test live (no rule change yet)**
1. Don't chase a 5-minute spike. Require a 5-minute change of 0 to 1% (or at most about 3%) at entry.
2. Stop placement: 68% of trades ended at the -12% stop. Test whether the stop is inside normal noise for these pools before changing it.
3. Fewer, larger trades to cut cost drag. This is Risk Desk's call on sizing and needs its own backtest first.

Each one needs a replay with dev and held-out side by side, plus 30+ closed live trades, before any proposal goes to Manari.

## Scout: not getting rugged (written Oct 25, 2026)

Sources: the desk's `data/memedesk.sqlite` (tables `decisions`, `snapshots`, `marks`) for cycles 1-12 on Oct 25, plus public spot checks of `api.rugcheck.xyz/v1/tokens/{mint}/report`, `api.dexscreener.com/tokens/v1/solana/{mint}` and GeckoTerminal pools around 12:24 ET. It covers one day of live data, so treat it as examples, not proof.

### How rugs happened on our data
1. **The liquidity gets pulled from a brand-new pool.**
   - GNUT: the pool was created at 10:39:02 ET. The baseline bought at 10:41:47 with $19,996 of liquidity, and by 10:43:19 liquidity was $4.58. Both sides of the pool were emptied: 95.81 SOL went to 0.000066 SOL (snapshot 132).
   - ELON: 1.4 minutes old when the baseline bought it, and liquidity went from $18,859 to $12.76 (snapshot 208).
   - Warning signs beforehand: the pool was under 2 minutes old, liquidity was just under $20k, and RugCheck showed 0% of the LP locked (checked after the rug).
   - The quoted price stayed flat while the pool was empty. A price-only exit logged ELON as a "+71.9% take-profit" when it really lost $16.44.
2. **Insiders dump on the buyers.**
   - BASE passed every gate (LP 100% locked, top-10 holders at 4.17%, mint and freeze authority revoked). Between 10:30:50 and 10:34:57 its liquidity went from $25,208 to $2,551 and its price fell 97.8% (snapshots 91 to 97).
   - Token reserves rose from 135M to 905M while SOL fell from 124 to 7. That means holders sold into the pool; the liquidity wasn't pulled.
   - Warning sign: at 10:11, RugCheck's `insiderNetworks` held about 41% of supply across 59 wallets (snapshot 20). The top-10 holder check can't see supply spread over many wallets.
3. **Authorities left on and LP left unlocked.** These are caught today.
   - ZGAP: mint authority still enabled.
   - AMC: 0% of the LP locked.
   - Both were blocked in cycle 12.

### What our gates catch
| Gate | Catches | Evidence |
|---|---|---|
| Pair age at least 30 minutes | Fresh-pool liquidity pulls | Both drains were pools under 2 minutes old. It's the top block by token count (160 tokens, cycles 1-11). |
| Liquidity at least $20k | Thin pools | Of 17 blocked tokens with follow-up data, 4 later lost 40% or more of liquidity, against 1 of 13 passed tokens. |
| LP at least 95% burned or locked | The creator pulling the LP | Blocked AMC. Only tested on 15 tokens so far. |
| Mint and freeze authority revoked | New tokens minted, holders frozen | Blocked ZGAP. Freeze has never blocked anything yet. |
| Top-10 holders under 30% | Plain whale concentration | Missed BASE, because the insiders were spread across many wallets. |
| Missing data counts as a skip | Running blind | 159 tokens were skipped on missing data, none allowed. |

### Gaps and proposals (not live; each needs replay plus Manari's approval)
- **Insider-cluster gate** on RugCheck's `insiderNetworks` share of supply. The evidence is thin: BASE was at 41% and dumped, while CATTLEAXE was at 43% and held at 0.92x. Spy uses this section for wallet filtering.
- **If the traded pair isn't in RugCheck's market list,** count it as missing data instead of falling back to another market (`scout.py`).
- **Exits should use the sell value after slippage,** not the quoted price (Gambler's lane, flagged).
- **Not covered yet:** honeypots that can't be sold, dev-wallet selling, and fake volume. Our data has no examples of these yet, and the 2-week backtest can't show them, because dead coins are missing from free history and RugCheck's state today gets applied backwards.

## Risk Desk: sizing, fees, sample size, single-coin loss
Source for every number below: `backtest/trades.csv` (run `desk_150_full`, 184 trades, Oct 11–25) and `memedesk/config.py`. This is a survivor-biased backtest (see SCORECARD.md), so treat it as the best case.

**1. Fees set a minimum position size.** The cost model charges a flat $0.05 network fee per swap, plus 0.30% AMM fee and about 0.50% slippage each way. A round trip costs about $0.10 + 1.6% of the position.
- At $2.76 (the average desk position on a $150 book) that's about 5.2% per trade. Fees and slippage were $26.41 of the $28.51 loss.
- At $20 it's about 2.1%. At $50 it's about 1.8%. Above roughly $20, the fixed fee stops mattering and the percentage costs dominate.
- Rule of thumb: if position size × target edge per trade doesn't clear about $0.15, don't trade. On $150 at 2% sizing it never does.
- Bigger positions are not a free fix. $20 on a $150 book is 13% per coin, which is about 6.7 times today's 2% cap. Changing sizing means changing a risk limit, and that goes to Nico.

**2. Breakeven win rate.** Desk average win was +$1.02 and average loss −$0.55, so wins were 1.85 times the size of losses. Breakeven is 1 / (1 + 1.85) ≈ 35%. The desk won 25%.

**3. How many trades before we trust a result.**
- With a 25% win rate, the error on the win rate is about ±8 points at 30 trades, ±5 at 75 and ±4 at 120.
- That means 30 trades is enough to tell whether something is badly broken. It is not enough to show a 25% win rate has become 35%. That takes about 75 to 100 closed trades, on data the rule wasn't tuned on.
- Concentration check: the desk's top 5 trades made +$19.74. Without them, the result goes from −$28.51 to −$48.25. Before trusting any result, look at it with the best trade removed.

**4. Keeping one coin from sinking the book.**
- Stops don't hold in a drain. The 2 liquidity-drop exits lost −101% and −102% of their positions (NODF −$2.65). The −12% stop doesn't protect against a pool being pulled. The real cap on one coin is position size: 2% of the book.
- The 126 stop-losses (−$68.05) cost more than the 33 take-profits made (+$43.87). The bleed comes from many small losses, not one big one.
- The breakers that stay in place: 5% daily loss, 4 losses in a row (then a 240-minute cooldown), a 40% liquidity drop from entry (forced exit), and data older than 120 seconds (no entries).
- Missing data always counts as a no.

**Gate for any proposal (my veto applies):** a change has to be replayed with full costs at the actual book size and hold up on held-out data. It also has to hold up on the live desk's snapshots at about 75 or more closed trades, and it can't weaken a breaker. If a proposal rests on the survivor backtest alone, I'll veto it.

## Spy: following the money (wallet copying)

**How the desk does it now.** Spy pulls the last ~300 swaps per trending pool from GeckoTerminal `/pools/{pool}/trades`. It scores each wallet on sells minus buys in that window (min $100 realized, min 20% ROI), copies the wallet's buys only after Scout's gates pass, skips a copy that fills >15% above the leader's entry, sells when the leader sells, caps each wallet at 5% of the book, and demotes after 3 straight losses (`memedesk/spy.py`, `config.py`).

**What the data says (all from `data/memedesk.sqlite`, 2026-10-25):**
- Stored trade history covers 9 pools and 18,418 swaps by 6,713 wallets. Only 252 wallets traded in 2+ pools and 49 in 3+.
- Under the current scoring, 24 wallets qualify. Of those, 20 were profitable in 1 pool, 3 in 2 pools and 1 in 3 pools.
- All 6 tracked wallets earned their spot from a single pool. That's one trade's worth of evidence each.
- Some scores are not believable. `CsvgnKm2` shows 830x ROI and tracked wallet `6LdX4CNZ` shows 616x. The likeliest cause is a tiny buy inside the window followed by selling a bag bought before the window, which counts as "profit". So the score can't tell a smart trader from someone who already held the bag, possibly an insider.
- Live copy record: 2 copies, both of `1T5bSj2u` on Marvin. One closed via mirror exit at -$0.52 and one is open. Fills were +6.0% and -3.6% vs the leader, inside the 15% cap. That's far too few to judge.

**Lucky vs. consistent.** One window can't separate skill from luck. A wallet should earn copy status only with profit across several unrelated pools. The free data supports this, since 49 wallets already appear in 3+ pools, but our stored history is only today's 9 pools. Building a real record takes days of tracking, not a bigger single window.

**Filtering out bots, insiders and wash traders, using what's free:**
- **Bots:** wallets with >30 trades in a window are already excluded (`max_wallet_trades_in_window`).
- **Bag sellers or insiders:** treat any ROI above ~10x in one window as unverified, since the wallet sold more than we saw it buy.
- **Insider networks:** RugCheck's `insiderNetworks` field gives only network size and token amount, not member addresses (checked against the 74 stored reports). So we can't match our wallets to insider groups from the stored data. Scout's insider-share gate on the token is the defense here. See Scout's section.
- **Wash trading:** not measured yet. A sign of it is a wallet buying and selling the same pool repeatedly at a flat price.

**When to drop a wallet.** The current rule is 3 losses in a row. Also drop any wallet that has had no copyable buy for ~2 days, so stale wallets don't take up the 8 active slots.

**Proposals (need a replay test and Manari's OK, not yet applied):**
1. Activate a wallet only after it's profitable in 3+ distinct pools, and track the others as candidates until then.
2. Reject any single-window ROI above 10x as unverified.
3. Drop my earlier "insider-group" proposal. The free data can't support it at the wallet level.

*Limits:* there's no free full wallet history (Birdeye 401, GMGN 403, public RPC 429 per Manari's source-health notes), so Spy can't be backtested, as noted on the $150 scorecard.
