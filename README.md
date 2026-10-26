# memedesk — a paper-trading memecoin desk (Solana)

> **Primary runner is GitHub Actions; box loop is retired; run pull_state.sh before reading.**
> Since 2026-10-26 11:19:25 EDT (15:19:25 UTC) the only writer is the workflow
> [`desk cycle`](.github/workflows/cycle.yml) in https://github.com/manarinico/kastor-memedesk.
> On the box: `cd /workspace/memedesk && ./pull_state.sh` before reading `data/memedesk.sqlite`
> or `data/report_latest.txt`.

**Paper money only.** Nothing here can place a real order. There is no wallet, no private key,
no live order adapter, no sign-up, no paid API and no paid model. Every decision is a fixed,
deterministic rule. The book starts at **$1,000 of pretend money**.

The design follows the Loofyb0i article "Every Memecoin Bot Has a Failure Mode": every bot is
built around the specific way that kind of bot loses money, and that failure mode is guarded
with a hard rule that **rejects** a trade. It never just lowers a score.

## The four roles (one engine, `memedesk/engine.py`)

| Role | What it does | Failure mode it guards against |
|---|---|---|
| **Scout** (`scout.py`) | Finds fresh Solana launches and runs hard gates. Every rejection is saved with its reason. | Buying a rug or honeypot |
| **Gambler** (`gambler.py`) | Momentum entries on Scout-approved coins only | Sizing up after wins or losses ("revenge" or "hot hand" sizing) |
| **Spy** (`spy.py`) | Copies wallets that recently made money | Chasing a leader's entry; following a wallet that stopped being good |
| **Risk Desk** (`risk.py`) | Circuit breakers that stop new entries for the whole desk | A bad day, a losing streak, a pool getting drained, trading on old data |

### Scout hard gates
Each gate answers **ALLOW**, **BLOCK** or **INSUFFICIENT_DATA**. Missing, stale, NaN or
unparseable data gives INSUFFICIENT_DATA. That **never** counts as permission: the token is
skipped and the reason is logged.

* DexScreener snapshot no older than 120 s
* liquidity ≥ $20,000
* pair age between 30 minutes and 72 hours
* RugCheck report no older than 30 minutes and not flagged `rugged`
* mint authority revoked
* freeze authority revoked
* LP ≥ 95% burned or locked on the traded market
* top-10 holders own < 30%. Pool and AMM vault accounts are excluded, because counting them would reject every coin.

### Gambler signal (from DexScreener's pair data)
Enter when **all** of these hold:
* 1-hour price change between +5% and +60%
* 5-minute change ≥ 0
* 1-hour buys/sells ≥ 1.2
* 1-hour volume ≥ $10,000

**Size = 2% of the current book (equity), always.** It never depends on the last trade, and a test
enforces that. Exits:
* take-profit +25%
* stop-loss −12%
* time stop after 4 hours

There is a 2-hour cooldown before re-entering the same token, and at most 5 positions are open at once.

### Spy
* **Wallet discovery (weak but free):** each cycle pulls up to 300 recent trades from the
  top-3 trending Solana pools on GeckoTerminal. Every trade there names the wallet. A wallet
  qualifies if, inside that window:
  * its first trade was a buy
  * it made at most 30 trades (filters out bots and MEV)
  * buys minus sells came to at least +$100 realized
  * its ROI was ≥ 20%

  This is **window P&L, not a track record**. Free sources do not offer wallet history:
  Birdeye needs a key, GMGN is behind Cloudflare, and the public RPC rate-limits.
  You can also add your own wallets under `spy.manual_wallets` in a config file.
* **Tracking:** new buys and sells by watched wallets show up in the trade lists of the pools
  we poll. Events are deduplicated by transaction hash.
* **Rules:**
  * Skip if our simulated fill would be > 15% above the leader's entry price. The skip is logged
    with the wallet, leader price, our price and the lag in seconds.
  * Skip if the leader already sold.
  * Skip if Scout has not approved the token.
  * Skip if the signal is > 30 minutes old.
  * Mirror the leader's sell. A sell stamped *before* the leader's buy is ignored.
  * Cap each wallet at 5% of the book.
  * After 3 losing copies in a row the wallet is demoted and no longer copied.

### Risk Desk breakers
Each breaker is a pure function with its own unit test, plus an engine-level test that fails
if the breaker stops firing. These were checked by mutating each breaker to "always ALLOW":
its tests fail.

* **Daily loss:** the desk book is down ≥ 5% since the first cycle of the New York day → no new entries.
* **Consecutive losses:** 4 losing closes in a row → no new entries for 4 hours.
* **Liquidity drop:** a held token's pool liquidity is down ≥ 40% from our entry → forced exit, entry blocked.
* **Stale feed:** the newest DexScreener data is older than 120 s, or its timestamp is missing or in the future → no new entries.
* **Position caps (r6, live since 2026-10-26 10:45:25 EDT):** a Gambler or Spy entry is skipped if the desk's open cost basis in that token would exceed 4% of current desk equity (`cap_token_4pct`), or total desk open cost basis would exceed 20% (`cap_total_20pct`). The baseline book is excluded. Tests: `tests/test_caps.py`.

### Paper fills (costs)
* **Buy:** fee = 0.30% DEX fee + $0.05 network fee. Slippage = 0.50% base + price impact
  (`spend / (liquidity/2)`), capped at 90%.
* **Sell:** the same fee and slippage model.

**Open positions are valued at liquidation value**: a paper sell at the latest mark's price and
pool liquidity, with fees and slippage. A position whose pool was drained is therefore worth
almost nothing even when the quoted price looks fine. A real example: the baseline bought GNUT in a
~$20k Meteora pool that fell to ~$4.58 liquidity about 90 s later. If a position has no mark, it is
carried at its entry price and flagged unpriced. If it has a price but no liquidity number, it is
carried at mid and flagged.
The daily-loss breaker and the report both use this value.

All parameters live in `memedesk/config.py`.

## Data sources (checked with curl from this box on 2026-10-25)

| Source | Endpoint | Status | Used for |
|---|---|---|---|
| DexScreener | `/token-profiles/latest/v1`, `/token-boosts/latest/v1` | 200 | discovery |
| DexScreener | `/tokens/v1/solana/{up to 30 mints}`, `/token-pairs/v1/solana/{mint}`, `/latest/dex/search` | 200 | price, liquidity, age, momentum |
| GeckoTerminal | `/networks/solana/new_pools`, `/trending_pools` | 200 | discovery |
| GeckoTerminal | `/networks/solana/pools/{pool}/trades` | 200 | Spy (wallet-level trades) |
| RugCheck | `/v1/tokens/{mint}/report` | 200 | authorities, LP lock, holders, rugged flag |
| RugCheck | `/v1/stats/new_tokens`, `/v1/stats/recent` | 200 | discovery |
| RugCheck | `/v1/tokens/new_tokens` | **404** | not used |
| Jupiter | `lite-api.jup.ag/price/v3` | 200 | checked, not needed (DexScreener has prices) |
| Jupiter | `api.jup.ag/price/v2` | **404** | not used |
| Solana public RPC | `getHealth`, `getAccountInfo`, `getSignaturesForAddress` | 200 | not needed in the loop |
| Solana public RPC | `getTokenLargestAccounts` | **429** | not used; RugCheck gives holders |
| Birdeye | public-api | **401 (needs key)** | not used |
| GMGN | | **403 (Cloudflare)** | not used |
| pump.fun frontend-api | | **403 (Cloudflare)** | not used |

GeckoTerminal's free tier rate-limits hard: calls 2.2 s apart got 429s. The feed spaces Gecko calls
6 s apart and retries once after 15 s. That is why a cycle takes about 1 minute. An occasional 429
still happens; it is stored as a failed snapshot, and that pool's trades are skipped for the cycle.

`python -m memedesk probe` re-checks the sources the engine uses.

## Data integrity
* **Snapshots are immutable.** Every API response, including failures, is stored once with
  source, units, fetch time, receipt time, an id and a payload hash. SQLite triggers reject UPDATE
  and DELETE.
* Every decision row records its gate state (ALLOW, BLOCK or INSUFFICIENT_DATA), a reason, the
  **snapshot id** it relied on and the **rules version**. Every position also links to its entry
  decision and snapshot.
* The **rules version** is `RULES_VERSION + hash of the full config`, so changing any parameter
  changes the version. Bump `RULES_VERSION` by hand when you change a rule on purpose.
* **Restart-safe and idempotent:**
  * Cash and equity are always recomputed from the positions table, so nothing is double-counted.
  * Each entry has a unique proposal key, and only one position per token per bot can be open.
  * Leader events are marked as seen in the same transaction as the copy.
  * Closes only apply to positions that are still open.
  * Price marks ignore observations older than the current one, so out-of-order data cannot roll a price back.
* Entry candidates are re-priced from DexScreener just before entries. The slower RugCheck and
  Gecko calls happen before that.
* **Per-cycle timing** is stored in `cycles.timings`:
  * fetch time per source
  * decision time
  * data age at the entry check
  * simulated fill lag (configured latency + data age)

## Baseline (does filtering help?)
A separate $1,000 **baseline** book takes the same momentum entries with the same exits and
sizing, but **without** Scout's safety gates or the desk breakers. It only keeps a $5k liquidity
floor and a stale-data check, so it can fill at all. The report compares the two. Until each side
has around 30+ closed trades, the comparison is noise and the report says so.

## Replay
`python -m memedesk replay` re-runs the same engine chronologically over the saved live snapshots:
* Each replayed cycle only sees snapshots received up to that moment.
* Fees, slippage and latency are configurable. Latency moves the **fill** to the latest saved
  price known at decision time + latency. It never changes the decision and never uses a price
  recorded after that moment.
* Cycles are split by time into **development** (first 70% by default) and **evaluation**
  (the rest). Each runs in its own fresh database, so tune on development and only look at
  evaluation at the end.

Replay can only be as good as what was recorded. Prices exist only at cycle times
(every 15–30 min), so intra-cycle stop-loss gaps aren't seen.

## Commands
```bash
cd /workspace/memedesk
.venv/bin/python -m memedesk run_cycle        # one scan/decide/update pass
.venv/bin/python -m memedesk report           # plain-English scorecard
.venv/bin/python -m memedesk replay --dev-frac 0.7 --fee-bps 30 --slip-bps 50 --latency-s 30
.venv/bin/python -m memedesk probe            # re-check the free APIs
.venv/bin/python -m pytest -q                 # tests
```
* Use a different database with `--db PATH` or `MEMEDESK_DB=PATH`. The default is `data/memedesk.sqlite`.
* Override any parameter with a JSON file: `MEMEDESK_CONFIG=my.json`, e.g. `{"gambler": {"stop_loss_pct": 10}}`.

## Schedule
Run every **15–30 minutes** (20 is a good middle). Each cycle takes ~1 minute because of
GeckoTerminal's rate limit. Running more often wouldn't help: the exits only see prices at cycle
time, and RugCheck and Gecko would rate-limit harder. Example cron line, which `flock` keeps from overlapping:
```
*/20 * * * * cd /workspace/memedesk && flock -n data/.lock .venv/bin/python -m memedesk run_cycle >> data/cron.log 2>&1
```

## Limits (read these)
* **Spy discovery is weak.** It uses a few hundred trades per hot pool, not wallet history. A
  "profitable" wallet may just have been lucky once. Copies are also limited to pools we poll, so
  the Spy sees a leader's trades only in those ~4–7 pools.
* **Exits are checked only at cycle time.** A coin can crash through the stop between cycles, and
  the paper fill happens at the next observed price, so the loss can be bigger than −12%. Both
  live stop-losses so far filled at −14.5%.
* **Fills are modelled, not real.** Real slippage on thin memecoin pools, MEV and failed
  transactions can be worse.
* **Positions with no fresh price are held**, not closed. The report shows how many are unpriced.
* pump.fun bonding-curve tokens have no liquidity number on DexScreener. They come out as
  INSUFFICIENT_DATA and are skipped.
* The baseline still gets the stale-data and liquidity-floor protection, so it is "no safety
  filters", not "no rules".
* A handful of cycles proves the plumbing, not the strategy. Judge it after weeks of data and
  ≥ 30 closed trades per bot, on the **evaluation** replay.

## Reliability fix (2026-10-25)
* **Why cycles went missing.** Only cycle output was printed at the very end of a successful
  run, and `flock -n` skips silently, so a skipped, crashed or killed cycle left nothing in
  `cron.log`. Per-request read timeouts (12s) existed, but nothing bounded a whole cycle. The
  box is also paused when no agent is active (the kernel log shows repeated "virtual machine
  fork" resumes, and uptime counts only ~7h since a boot two days ago). A pause during a request
  shows up as a huge latency, like the 499s `dex_tokens_refresh` in cycle 13.
* **Fetches** (`feed.py`): every request has a (10s connect, 12s read) timeout plus a 15s total
  cap, including reading the body. A 429 gets at most one retry after a pause of at most 15s, and
  only if the budget allows it. All fetches in a cycle share a **180s budget**. After that,
  calls aren't made and come back `ok=False "fetch budget exhausted"`. Scout records those tokens
  as INSUFFICIENT_DATA, just like a timeout. Once the budget is hit, the stale-feed check
  returns INSUFFICIENT_DATA, so no entry is made on partial data. These limits are code
  constants, not config, so the rules version/hash is unchanged.
* **Logging/locking** (`__main__.py`): `run_cycle` appends timestamped start and done/FAILED/
  SKIPPED lines to `data/cron.log` itself (`--log`), whether it is started by the loop, cron, a
  routine or by hand. It takes its own non-blocking lock on `data/.cycle.lock`, so it is safe to
  trigger at any time. On error or SIGTERM it marks its `cycles` row `failed`.
* **Loop** (`run_loop.sh`): `flock -n -E 75 data/.lock timeout -k 30 600 python -m memedesk run_cycle`.
  It logs a start line (with uptime, which helps spot box pauses) and an end line with the exit code.


## Runner: GitHub Actions (since 2026-10-26)
* **Where it runs:** `.github/workflows/cycle.yml`, cron `*/20 * * * *` plus manual `workflow_dispatch`,
  concurrency group `desk` (never two cycles at once, never cancelled mid-cycle), job timeout 10 min,
  cycle timeout 450 s. Each run: restore state -> integrity check -> `python -m memedesk run_cycle`
  (same entry as the old `run_loop.sh`) -> `python -m memedesk report` -> integrity check -> push state.
* **Host is recorded:** the run sets `MEMEDESK_HOST=github-actions`; it appears on the cron log lines
  (`run_cycle start (..., host github-actions)`, `cycle N done ... host github-actions`) and in
  `kv` as `cycle_host:<cycle id>`. The cycles table has no host column (unchanged schema). Timestamps are real.
* **State:** orphan branch `state`, exactly one commit, force-pushed each run with a lease on the commit
  the run started from (a second writer makes the push fail instead of overwriting). It holds
  `memedesk.sqlite.gz` (every table; `snapshots` trimmed to the last 6 h of receipts, which covers
  everything the live engine reads: this cycle's payloads and RugCheck reports <= 30 min old),
  `report_latest.txt`, the last 5,000 lines of `cron.log`, and `STATE.json`.
  Restore fails closed: no branch, a bad gzip, a failed `integrity_check` or a DB with 0 cycles stops the run.
* **Full snapshot history** stays on the box: `data/archive/memedesk_full_pre_actions_20261026.sqlite`
  (read-only copy at cutover, 773 snapshots) and `data/archive/snapshots_archive.sqlite`, which
  `pull_state.sh` keeps appending to. Pull at least every ~5 h or older snapshots are lost.
  Replay needs the archive DB, not the slim one. The report's "Snapshots stored" now counts the slim DB.
* **Late/missed cycles:** GitHub's scheduler is best-effort. Runs are often 5-30+ min late and can be
  skipped at busy times. Nothing is backfilled; delays show up as gaps in `cycles.started_at`.
* **Box:** `run_loop.sh` is retired and refuses to start (override `MEMEDESK_ALLOW_BOX_LOOP=1`
  only if Actions is disabled first). `pull_state.sh` installs the latest state atomically for
  read-only consumers and refreshes `data/report_latest.txt`, `data/cron_actions.log` and `data/STATE.json`.
* **Paper only:** no secrets, keys or wallets in the repo or the workflow; the only token used is
  the run's own `GITHUB_TOKEN` (contents: write) to push the `state` branch.
