"""Fetch timeouts, the per-cycle fetch budget, and how a cycle degrades when it runs out.
Missing data must end as INSUFFICIENT_DATA/BLOCK, never ALLOW; every run_cycle exit logs a line."""
import json

import pytest
import requests

from memedesk import __main__ as cli, engine, feed as feedmod
from memedesk.feed import FetchResult, LiveFeed
from memedesk.gates import ALLOW, INSUFFICIENT
from conftest import FakeFeed, good_token


class Mono:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Resp:
    def __init__(self, status=200, body=b"[]"):
        self.status_code, self.body = status, body

    def iter_content(self, n):
        yield self.body

    def close(self):
        pass


class HangingSession:
    """Every request 'hangs' until its read timeout fires (advances the fake monotonic clock)."""
    def __init__(self, mono, status=None):
        self.mono, self.status, self.calls, self.headers = mono, status, [], {}

    def get(self, url, timeout=None, stream=False):
        self.calls.append((url, timeout, stream))
        if self.status:
            return Resp(self.status)
        self.mono.t += timeout[1]
        raise requests.exceptions.ReadTimeout("Read timed out.")


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(feedmod.time, "sleep", lambda s: slept.append(s))
    return slept


def test_every_request_has_connect_and_read_timeout(cfg, no_sleep):
    mono = Mono()
    s = HangingSession(mono)
    f = LiveFeed(cfg, session=s, mono=mono)
    r = f.rug_report("MINT")
    assert not r.ok and "ReadTimeout" in r.error
    (url, timeout, stream), = s.calls
    assert isinstance(timeout, tuple) and len(timeout) == 2
    assert 0 < timeout[0] <= 15 and 0 < timeout[1] <= 15
    assert stream is True   # body read under a total-time cap too


def test_budget_exhausted_degrades_to_missing_data_without_requests(cfg, no_sleep):
    mono = Mono()
    s = HangingSession(mono)
    f = LiveFeed(cfg, session=s, mono=mono, budget_s=30)
    res = f.discover() + f.dex_tokens([f"M{i}" for i in range(90)])
    assert all(not r.ok for r in res)
    skipped = [r for r in res if r.error.startswith("fetch budget exhausted")]
    assert len(s.calls) <= 3                 # 12s timeouts eat a 30s budget in 3 calls
    assert len(skipped) == len(res) - len(s.calls)
    assert mono.t - 1000.0 <= 30 + 15        # never runs far past the budget


def test_429_retry_is_capped_and_skipped_without_budget(cfg, no_sleep):
    mono = Mono()
    s = HangingSession(mono, status=429)
    f = LiveFeed(cfg, session=s, mono=mono, budget_s=100)
    f.retry_wait_s = 999
    r = f.pool_trades("POOL")
    assert r.error == "HTTP 429" and len(s.calls) == 2
    assert max(no_sleep) <= feedmod.MAX_RETRY_WAIT_S
    mono.t += 90                              # 10s left < retry wait
    s.calls.clear()
    assert f.pool_trades("POOL2").error == "HTTP 429" and len(s.calls) == 1


class BudgetAfterPrices(FakeFeed):
    """Discovery and first prices arrive; the budget runs out before the pre-entry re-price."""
    def dex_tokens(self, mints, kind="dex_tokens"):
        if kind == "dex_tokens_refresh":
            t = self.clock()
            return [FetchResult("fake", kind, ",".join(mints), "fake://", "t", False, None,
                                "fetch budget exhausted (180s per cycle); not requested", t, t, 0.0)]
        return super().dex_tokens(mints, kind)


def test_engine_control_cycle_enters(conn, cfg, feed, clock):
    good_token(feed)
    res = engine.run_cycle(conn, cfg, feed, clock)
    assert res["summary"]["entries"] >= 1 and res["summary"]["fetch_budget_hit"] is None


def test_engine_never_allows_entries_after_budget_exhausted(conn, cfg, clock):
    f = BudgetAfterPrices(clock)
    good_token(f)
    res = engine.run_cycle(conn, cfg, f, clock)
    assert res["summary"]["fetch_budget_hit"]
    assert res["summary"]["entries"] == 0
    rows = conn.execute("SELECT book, bot, state, details FROM decisions WHERE stage='entry'").fetchall()
    assert rows and all(r["state"] != ALLOW for r in rows)
    gates = json.loads(rows[0]["details"])["gates"]
    assert any(g["name"] == "stale_feed" and g["state"] == INSUFFICIENT for g in gates)
    assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0  # baseline too


def test_engine_all_sources_missing_is_insufficient(conn, cfg, feed, clock):
    good_token(feed)
    feed.fail = {"all"}
    res = engine.run_cycle(conn, cfg, feed, clock)
    stale = [b for b in res["summary"]["desk_breakers"] if b["name"] == "stale_feed"][0]
    assert stale["state"] == INSUFFICIENT
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE state='ALLOW' AND stage IN ('scout','entry')").fetchone()[0] == 0


def test_cli_logs_failures_and_lock_skips(tmp_path, monkeypatch):
    log, dbp = tmp_path / "cron.log", tmp_path / "x.sqlite"

    def boom(cfg):
        raise RuntimeError("feed exploded")
    monkeypatch.setattr(cli, "LiveFeed", boom)
    assert cli.main(["--db", str(dbp), "--log", str(log), "run_cycle"]) == 1
    text = log.read_text()
    assert "run_cycle start" in text and "run_cycle FAILED" in text and "feed exploded" in text

    import fcntl
    with open(tmp_path / ".cycle.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert cli.main(["--db", str(dbp), "--log", str(log), "run_cycle"]) == 0
    assert "run_cycle SKIPPED" in log.read_text()
