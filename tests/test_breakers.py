"""Every Risk Desk breaker has a pure test AND an engine-level test that fails if it stops firing."""
import json

from memedesk import db, engine, risk
from memedesk.gates import ALLOW, BLOCK, INSUFFICIENT
from conftest import good_token, make_pair


def _entries(conn, bot=None):
    q = "SELECT COUNT(*) FROM positions WHERE book='desk'" + (f" AND bot='{bot}'" if bot else "")
    return conn.execute(q).fetchone()[0]


def _trips(conn, name):
    return conn.execute("SELECT COUNT(*) FROM breaker_trips WHERE breaker=?", (name,)).fetchone()[0]


# ---- daily loss
def test_daily_loss_pure(cfg):
    assert risk.daily_loss_breaker(950.0, 1000.0, cfg).state == BLOCK      # exactly -5%
    assert risk.daily_loss_breaker(940.0, 1000.0, cfg).state == BLOCK
    assert risk.daily_loss_breaker(951.0, 1000.0, cfg).state == ALLOW
    assert risk.daily_loss_breaker(950.0, None, cfg).state == INSUFFICIENT


def test_daily_loss_blocks_entries_in_engine(conn, cfg, feed, clock):
    good_token(feed)
    day = engine._local_day(clock())
    db.kv_set(conn, f"day_start_equity:desk:{day}", 1100.0)  # book is $1000 => -9.1% today
    engine.run_cycle(conn, cfg, feed, clock)
    assert _entries(conn) == 0
    assert _trips(conn, "daily_loss") == 1


# ---- consecutive losses
def test_consecutive_loss_pure(cfg, clock):
    now = clock()
    assert risk.consecutive_loss_breaker([-1, -1, -1, -1], now - 60, now, cfg).state == BLOCK
    assert risk.consecutive_loss_breaker([-1, -1, -1, 5, -1], now - 60, now, cfg).state == ALLOW
    cool = cfg["risk"]["consecutive_cooldown_min"] * 60
    assert risk.consecutive_loss_breaker([-1] * 4, now - cool - 1, now, cfg).state == ALLOW


def test_consecutive_losses_block_entries_in_engine(conn, cfg, feed, clock):
    good_token(feed)
    for i in range(4):
        conn.execute("INSERT INTO positions(proposal_key,book,bot,mint,qty,entry_mid,entry_px,entry_cost_usd,entry_ts,"
                     "status,exit_ts,exit_proceeds_usd,pnl_usd) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f"old{i}", "desk", "gambler", f"OLD{i}", 1, 1, 1, 10.0, clock() - 7200, "closed",
                      clock() - 600 + i, 9.0, -1.0))
    engine.run_cycle(conn, cfg, feed, clock)
    assert _entries(conn, "gambler") == 4  # only the 4 seeded ones
    assert _trips(conn, "consecutive_losses") == 1


# ---- liquidity drop
def test_liquidity_drop_pure(cfg):
    assert risk.liquidity_drop_breaker(100_000, 60_000, cfg).state == BLOCK   # -40%
    assert risk.liquidity_drop_breaker(100_000, 61_000, cfg).state == ALLOW
    assert risk.liquidity_drop_breaker(100_000, None, cfg).state == INSUFFICIENT


def test_liquidity_drop_forces_exit_in_engine(conn, cfg, feed, clock):
    m = good_token(feed)
    engine.run_cycle(conn, cfg, feed, clock)
    assert _entries(conn, "gambler") == 1
    clock.advance(900)
    feed.pairs[m] = make_pair(m, liq=40_000.0, now=clock() - 900)  # liquidity -60%, price flat
    engine.run_cycle(conn, cfg, feed, clock)
    p = conn.execute("SELECT status, exit_reason FROM positions WHERE bot='gambler'").fetchone()
    assert p["status"] == "closed" and p["exit_reason"].startswith("liquidity_drop_breaker")
    assert _trips(conn, "liquidity_drop") == 1


# ---- stale feed
def test_stale_feed_pure(cfg, clock):
    now = clock()
    assert risk.stale_feed_breaker(now - cfg["risk"]["stale_feed_s"] - 1, now, cfg).state == BLOCK
    assert risk.stale_feed_breaker(now - 5, now, cfg).state == ALLOW
    assert risk.stale_feed_breaker(None, now, cfg).state == INSUFFICIENT


def test_stale_feed_blocks_entries_in_engine(conn, cfg, feed, clock):
    good_token(feed)
    feed.receipt_lag = 600  # every response is 10 minutes old
    engine.run_cycle(conn, cfg, feed, clock)
    assert _entries(conn) == 0
    assert _trips(conn, "stale_feed") == 1
