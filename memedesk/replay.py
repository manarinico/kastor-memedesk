"""Replay: re-run the exact same engine chronologically over saved snapshots.

* Only data received at/before each decision moment is visible to the decision.
* Fees, slippage and latency come from the (overridable) config. Latency is applied to
  the FILL only: the fill uses the first saved observation at/after decision+latency.
* Development and evaluation periods are run as two separate fresh books so tuning on
  dev can be checked on untouched eval data.
"""
import bisect
import json
import os

from . import db
from .engine import run_cycle
from .feed import DISCOVERY_KINDS, FetchResult
from .gates import num


class ReplayClock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def build_pair_index(src):
    idx = {}
    for row in src.execute("SELECT id, receipt_ts, payload FROM snapshots WHERE kind IN ('dex_tokens','dex_tokens_refresh') AND ok=1"):
        try:
            pairs = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        for p in pairs if isinstance(pairs, list) else []:
            pa, px = p.get("pairAddress"), num(p, "priceUsd")
            if pa and px:
                idx.setdefault(pa, []).append((row["receipt_ts"], px, num(p, "liquidity", "usd"), row["id"]))
    for v in idx.values():
        v.sort()
    return idx


class ReplayFeed:
    def __init__(self, src, cycle_id, clock, pair_index):
        self.src, self.cid, self.clock, self.idx = src, cycle_id, clock, pair_index

    def _res(self, row):
        self.clock.t = max(self.clock.t, row["receipt_ts"] or self.clock.t)
        return FetchResult(row["source"], row["kind"], row["key"], row["url"], row["units"], bool(row["ok"]),
                           json.loads(row["payload"]) if row["payload"] else None, row["error"],
                           row["fetch_ts"], row["receipt_ts"], row["latency_ms"])

    def _missing(self, kind, key):
        t = self.clock.t
        return FetchResult("replay", kind, key, "", "", False, None, "not in recorded data", t, t, 0)

    def _rows(self, kind):
        return self.src.execute("SELECT * FROM snapshots WHERE cycle_id=? AND kind=? ORDER BY seq",
                                (self.cid, kind)).fetchall()

    def discover(self):
        out = []
        for k in DISCOVERY_KINDS:
            out += [self._res(r) for r in self._rows(k)]
        return out

    def dex_tokens(self, mints, kind="dex_tokens"):
        return [self._res(r) for r in self._rows(kind)]

    def rug_report(self, mint):
        r = self.src.execute("SELECT * FROM snapshots WHERE cycle_id=? AND kind='rug_report' AND key=? ORDER BY seq LIMIT 1",
                             (self.cid, mint)).fetchone()
        if r is None:
            r = self.src.execute("SELECT * FROM snapshots WHERE kind='rug_report' AND key=? AND ok=1 AND receipt_ts<=? "
                                 "ORDER BY receipt_ts DESC LIMIT 1", (mint, self.clock.t)).fetchone()
        return self._res(r) if r else self._missing("rug_report", mint)

    def pool_trades(self, pool):
        r = self.src.execute("SELECT * FROM snapshots WHERE cycle_id=? AND kind='pool_trades' AND key=? ORDER BY seq LIMIT 1",
                             (self.cid, pool)).fetchone()
        return self._res(r) if r else self._missing("pool_trades", pool)

    def mid_at(self, pair_address, ts):
        lst = self.idx.get(pair_address)
        if not lst:
            return None
        # the most recent observation known at fill time (decision time + latency); never a
        # later one - jumping to the next recorded cycle would be look-ahead, not latency
        i = bisect.bisect_right(lst, (ts, float("inf"))) - 1
        if i < 0:
            return None
        rts, px, liq, sid = lst[i]
        return {"price": px, "liq": liq, "sid": sid, "ts": rts}


def source_cycles(src, start=None, end=None):
    q = "SELECT id, started_at FROM cycles WHERE mode='live' AND status='done'"
    a = []
    if start:
        q += " AND started_at>=?"; a.append(start)
    if end:
        q += " AND started_at<?"; a.append(end)
    return [dict(r) for r in src.execute(q + " ORDER BY started_at", a)]


def replay(src_path, cfg, cycles, out_path):
    if os.path.exists(out_path):
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(out_path + ext):
                os.remove(out_path + ext)
    src = db.connect(src_path)
    idx = build_pair_index(src)
    out = db.connect(out_path)
    for c in cycles:
        clock = ReplayClock(c["started_at"])
        run_cycle(out, cfg, ReplayFeed(src, c["id"], clock, idx), clock=clock, mode="replay",
                  source_cycle_id=c["id"])
    return out


def split(cycles, dev_frac):
    n = int(len(cycles) * dev_frac)
    return cycles[:n], cycles[n:]
