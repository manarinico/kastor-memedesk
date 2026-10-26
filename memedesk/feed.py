"""Market data feeds. LiveFeed = free public HTTP APIs (verified from this box).
Every call returns a FetchResult that the engine persists as an immutable snapshot."""
import json
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
RUG = "https://api.rugcheck.xyz/v1"

# minimum seconds between calls per host. GeckoTerminal answered HTTP 429 at one call per 2.2s
# from this box, so we stay at ~10/min and retry a 429 once after a pause.
MIN_INTERVAL = {"api.geckoterminal.com": 6.0, "api.rugcheck.xyz": 0.4, "api.dexscreener.com": 0.25}

# Reliability limits (plumbing, not trading rules; deliberately kept out of cfg so the
# rules-version hash does not change). Every request gets a connect timeout and a read
# timeout, a hard cap on total time per request (a server trickling bytes can't hold us),
# and every cycle's fetches share one budget. Past the budget, calls are not made and come
# back ok=False ("fetch budget exhausted"); missing data is never permission downstream.
CONNECT_TIMEOUT_S = 10.0
MAX_REQUEST_S = 15.0          # total wall time per request, including reading the body
FETCH_BUDGET_S = 180.0        # all fetches in one cycle
MAX_RETRY_WAIT_S = 15.0       # one retry on HTTP 429, never longer than this pause

DISCOVERY_KINDS = ("dex_profiles", "dex_boosts", "gecko_new_pools", "gecko_trending", "rug_new_tokens")


@dataclass
class FetchResult:
    source: str
    kind: str
    key: str
    url: str
    units: str
    ok: bool
    payload: object = None
    error: str = None
    fetch_ts: float = None     # request sent (unix s)
    receipt_ts: float = None   # response received (unix s)
    latency_ms: float = None
    snapshot_id: int = None    # set by the engine once persisted


class LiveFeed:
    def __init__(self, cfg, session=None, budget_s=FETCH_BUDGET_S, mono=time.monotonic):
        import requests
        self.read_timeout = min(float(cfg["http"]["timeout_s"]), MAX_REQUEST_S)
        self.connect_timeout = CONNECT_TIMEOUT_S
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": "memedesk-paper/1.0 (research, paper trading)",
                               "Accept": "application/json"})
        self._last = {}
        self.retry_wait_s = MAX_RETRY_WAIT_S
        self._mono = mono
        self.start_budget(budget_s)

    def start_budget(self, budget_s=FETCH_BUDGET_S):
        """(Re)start the per-cycle fetch budget. Called at construction, i.e. cycle start."""
        self.budget_s = budget_s
        self.deadline = self._mono() + budget_s
        self.budget_skips = 0

    def budget_left(self):
        return self.deadline - self._mono()

    def _skip(self, source, kind, key, url, units, why):
        self.budget_skips += 1
        t = time.time()
        return FetchResult(source, kind, key, url, units, False, None, why, t, t, 0.0)

    def _get(self, source, kind, key, url, units):
        r = self._get_once(source, kind, key, url, units)
        if not r.ok and r.error == "HTTP 429":
            wait = min(self.retry_wait_s, MAX_RETRY_WAIT_S)
            if self.budget_left() < wait + 1.0:
                return r  # no time for a retry: the 429 stands as missing data
            time.sleep(wait)
            r = self._get_once(source, kind, key, url, units)
        return r

    def _get_once(self, source, kind, key, url, units):
        host = urlparse(url).netloc
        left = self.budget_left()
        if left <= 1.0:
            return self._skip(source, kind, key, url, units,
                              f"fetch budget exhausted ({self.budget_s:.0f}s per cycle); not requested")
        gap = MIN_INTERVAL.get(host, 0) - (time.time() - self._last.get(host, 0))
        if gap > 0:
            time.sleep(min(gap, max(0.0, left - 1.0)))
        left = self.budget_left()
        if left <= 1.0:
            return self._skip(source, kind, key, url, units,
                              f"fetch budget exhausted ({self.budget_s:.0f}s per cycle); not requested")
        cap = min(MAX_REQUEST_S, left)
        t0 = time.time()
        m0 = self._mono()
        try:
            read_to = min(self.read_timeout, cap)
            resp = self.s.get(url, timeout=(min(self.connect_timeout, cap), read_to), stream=True)
            try:
                if resp.status_code != 200:
                    t1 = time.time()
                    self._last[host] = t1
                    return FetchResult(source, kind, key, url, units, False, None,
                                       f"HTTP {resp.status_code}", t0, t1, (t1 - t0) * 1000)
                chunks = []
                for chunk in resp.iter_content(65536):
                    chunks.append(chunk)
                    if self._mono() - m0 > cap:
                        raise TimeoutError(f"request exceeded {cap:.1f}s total")
                body = b"".join(chunks)
            finally:
                resp.close()
            t1 = time.time()
            self._last[host] = t1
            return FetchResult(source, kind, key, url, units, True, json.loads(body), None,
                               t0, t1, (t1 - t0) * 1000)
        except Exception as e:  # timeouts, DNS, bad JSON -> recorded, never permission
            t1 = time.time()
            self._last[host] = t1
            return FetchResult(source, kind, key, url, units, False, None,
                               f"{type(e).__name__}: {str(e)[:200]}", t0, t1, (t1 - t0) * 1000)

    def discover(self):
        return [
            self._get("dexscreener", "dex_profiles", "latest", f"{DEX}/token-profiles/latest/v1", "list[token profile]"),
            self._get("dexscreener", "dex_boosts", "latest", f"{DEX}/token-boosts/latest/v1", "list[token boost]"),
            self._get("geckoterminal", "gecko_new_pools", "solana", f"{GECKO}/networks/solana/new_pools", "pools; prices USD"),
            self._get("geckoterminal", "gecko_trending", "solana", f"{GECKO}/networks/solana/trending_pools", "pools; prices USD"),
            self._get("rugcheck", "rug_new_tokens", "latest", f"{RUG}/stats/new_tokens", "list[mint]"),
        ]

    def dex_tokens(self, mints, kind="dex_tokens"):
        out = []
        mints = list(dict.fromkeys(mints))
        for i in range(0, len(mints), 30):
            chunk = mints[i:i + 30]
            out.append(self._get("dexscreener", kind, ",".join(chunk),
                                 f"{DEX}/tokens/v1/solana/{','.join(chunk)}",
                                 "pairs; priceUsd USD; liquidity.usd USD; volume USD; priceChange %; pairCreatedAt ms"))
        return out

    def rug_report(self, mint):
        return self._get("rugcheck", "rug_report", mint, f"{RUG}/tokens/{mint}/report",
                         "report; topHolders[].pct %; markets[].lp.lpLockedPct %")

    def pool_trades(self, pool):
        return self._get("geckoterminal", "pool_trades", pool,
                         f"{GECKO}/networks/solana/pools/{pool}/trades",
                         "last<=300 trades; volume_in_usd USD; block_timestamp ISO-8601 UTC")

    def mid_at(self, pair_address, ts):
        """Live mode cannot see the future: latency is recorded, not price-modelled."""
        return None
