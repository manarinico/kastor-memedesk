import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memedesk import db  # noqa: E402
from memedesk.config import load_config  # noqa: E402
from memedesk.feed import FetchResult  # noqa: E402

T0 = 1_792_900_000.0  # fixed fake "now"
WSOL = "So11111111111111111111111111111111111111112"


class FakeClock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def make_pair(mint, pair_addr=None, price=1.0, liq=100_000.0, age_min=120.0, h1=10.0, m5=1.0,
              buys=300, sells=200, vol_h1=50_000.0, now=T0, symbol=None):
    return {"chainId": "solana", "dexId": "pumpswap", "pairAddress": pair_addr or f"POOL_{mint}",
            "baseToken": {"address": mint, "symbol": symbol or mint[:6]},
            "quoteToken": {"address": WSOL, "symbol": "SOL"},
            "priceUsd": str(price), "liquidity": {"usd": liq},
            "priceChange": {"m5": m5, "h1": h1}, "txns": {"h1": {"buys": buys, "sells": sells}},
            "volume": {"h1": vol_h1}, "pairCreatedAt": int((now - age_min * 60) * 1000)}


def make_rug(mint, pair_addr=None, mint_auth=None, freeze=None, lp=100.0, holders=None, rugged=False):
    pa = pair_addr or f"POOL_{mint}"
    holders = holders if holders is not None else [2.0] * 10
    th = [{"address": f"ACC{i}", "owner": f"OWN{i}", "pct": p, "insider": False} for i, p in enumerate(holders)]
    th.insert(0, {"address": "POOLVAULT", "owner": pa, "pct": 40.0})  # AMM pool: must be excluded
    return {"mint": mint, "mintAuthority": mint_auth, "freezeAuthority": freeze, "rugged": rugged,
            "markets": [{"pubkey": pa, "lp": {"lpLockedPct": lp, "lpLockedUSD": 1e5}}],
            "topHolders": th, "knownAccounts": {pa: {"name": "Pump Fun AMM", "type": "AMM"}}}


def gecko_trade(wallet, kind, mint, price, ts, tx, usd=100.0):
    a = {"tx_hash": tx, "tx_from_address": wallet, "kind": kind,
         "block_timestamp": dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "volume_in_usd": str(usd)}
    if kind == "buy":
        a.update(from_token_address=WSOL, to_token_address=mint, price_to_in_usd=str(price), price_from_in_usd="100")
    else:
        a.update(from_token_address=mint, to_token_address=WSOL, price_from_in_usd=str(price), price_to_in_usd="100")
    return {"id": tx, "type": "trade", "attributes": a}


class FakeFeed:
    """Programmable feed. `pairs`: mint -> pair dict; `rugs`: mint -> report;
    `trades`: pool -> list of gecko trades; `fail`: set of kinds that time out;
    `receipt_lag`: seconds to subtract from receipt time (simulates stale data)."""

    def __init__(self, clock):
        self.clock = clock
        self.mints, self.pairs, self.rugs, self.trades = [], {}, {}, {}
        self.trending = []  # pool addresses
        self.fail = set()
        self.receipt_lag = 0.0
        self.calls = []
        self.drop_fields = {}  # mint -> list of keys to delete from the pair

    def _r(self, kind, key, payload):
        self.calls.append(kind)
        t = self.clock() - self.receipt_lag
        if kind in self.fail or "all" in self.fail:
            return FetchResult("fake", kind, key, "fake://", "test", False, None, "Timeout: simulated", t, t, 12000)
        return FetchResult("fake", kind, key, "fake://", "test", True, payload, None, t, t, 5)

    def discover(self):
        tp = {"data": [{"attributes": {"address": p}, "relationships": {"base_token": {"data": {"id": f"solana_{m}"}}}}
                       for p, m in self.trending]}
        return [self._r("dex_profiles", "latest", [{"chainId": "solana", "tokenAddress": m} for m in self.mints]),
                self._r("gecko_trending", "solana", tp)]

    def dex_tokens(self, mints, kind="dex_tokens"):
        out = []
        for m in mints:
            if m in self.pairs:
                p = dict(self.pairs[m])
                for k in self.drop_fields.get(m, []):
                    p.pop(k, None)
                out.append(p)
        return [self._r(kind, ",".join(mints), out)]

    def rug_report(self, mint):
        if mint not in self.rugs:
            return FetchResult("fake", "rug_report", mint, "", "", False, None, "HTTP 404",
                               self.clock(), self.clock(), 1)
        return self._r("rug_report", mint, self.rugs[mint])

    def pool_trades(self, pool):
        return self._r("pool_trades", pool, {"data": list(self.trades.get(pool, []))})

    def mid_at(self, pair_address, ts):
        return None


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def conn(tmp_path):
    return db.connect(str(tmp_path / "t.sqlite"))


@pytest.fixture
def cfg():
    return load_config({"costs": {"latency_s": 0.0}})


@pytest.fixture
def feed(clock):
    return FakeFeed(clock)


def good_token(feed, mint="MINTA", **kw):
    feed.mints.append(mint)
    feed.pairs[mint] = make_pair(mint, now=feed.clock(), **kw)
    feed.rugs[mint] = make_rug(mint)
    return mint
