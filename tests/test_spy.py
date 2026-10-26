import json

from memedesk import engine, spy
from memedesk.config import load_config
from memedesk.gates import ALLOW, BLOCK
from conftest import good_token, gecko_trade, make_pair

W = "LEADERWALLET1111111111111111111111111111111"


def spy_cfg(**kw):
    o = {"costs": {"latency_s": 0.0}, "spy": {"manual_wallets": [W]}, "gambler": {"min_h1_change_pct": 1000.0}}
    o["spy"].update(kw)
    return load_config(o)  # gambler disabled via impossible signal so only Spy trades


def test_max_chase_gate_pure(cfg):
    assert spy.chase_gate(1.15, 1.0, cfg).state == ALLOW
    assert spy.chase_gate(1.1501, 1.0, cfg).state == BLOCK
    assert spy.chase_gate(None, 1.0, cfg).state != ALLOW


def _setup(feed, clock, leader_px, cur_px):
    m = good_token(feed, price=cur_px)
    pool = feed.pairs[m]["pairAddress"]
    feed.trending = [(pool, m)]
    feed.trades[pool] = [gecko_trade(W, "buy", m, leader_px, clock() - 120, "TX1")]
    return m, pool


def test_spy_skips_when_fill_too_far_above_leader_and_logs_wallet_and_lag(conn, feed, clock):
    cfg = spy_cfg()
    _setup(feed, clock, leader_px=1.0, cur_px=1.30)
    engine.run_cycle(conn, cfg, feed, clock)
    assert conn.execute("SELECT COUNT(*) FROM positions WHERE bot='spy'").fetchone()[0] == 0
    r = conn.execute("SELECT details FROM decisions WHERE bot='spy' AND reason_code='max_chase' AND state='BLOCK'").fetchone()
    d = json.loads(r[0])
    assert d["wallet"] == W and abs(d["lag_s"] - 120) < 1


def test_spy_copies_mirrors_exit_and_dedupes(conn, feed, clock):
    cfg = spy_cfg()
    m, pool = _setup(feed, clock, leader_px=1.0, cur_px=1.02)
    engine.run_cycle(conn, cfg, feed, clock)
    assert conn.execute("SELECT COUNT(*) FROM positions WHERE bot='spy'").fetchone()[0] == 1
    # duplicate event delivered again next cycle -> no second copy
    clock.advance(600)
    engine.run_cycle(conn, cfg, feed, clock)
    assert conn.execute("SELECT COUNT(*) FROM positions WHERE bot='spy'").fetchone()[0] == 1
    # out-of-order: a SELL stamped BEFORE the leader's buy must not trigger a mirror exit
    feed.trades[pool].append(gecko_trade(W, "sell", m, 1.0, clock() - 600 - 500, "TX0"))
    clock.advance(600)
    engine.run_cycle(conn, cfg, feed, clock)
    assert conn.execute("SELECT status FROM positions WHERE bot='spy'").fetchone()[0] == "open"
    # real leader sell after the buy -> mirror exit
    feed.trades[pool].append(gecko_trade(W, "sell", m, 1.05, clock() - 10, "TX2"))
    clock.advance(60)
    engine.run_cycle(conn, cfg, feed, clock)
    r = conn.execute("SELECT status, exit_reason FROM positions WHERE bot='spy'").fetchone()
    assert r[0] == "closed" and r[1].startswith("mirror_exit")


def test_wallet_demoted_after_three_losing_copies(conn, feed, clock):
    cfg = spy_cfg()
    for i in range(3):
        m = good_token(feed, mint=f"MINT{i}", price=1.0)
        pool = feed.pairs[m]["pairAddress"]
        feed.trending = [(pool, m)]
        feed.trades[pool] = [gecko_trade(W, "buy", m, 1.0, clock() - 60, f"B{i}")]
        engine.run_cycle(conn, cfg, feed, clock)
        clock.advance(300)
        feed.pairs[m] = make_pair(m, price=0.7, now=clock())  # -30% -> stop loss (20%)
        engine.run_cycle(conn, cfg, feed, clock)
        feed.mints.remove(m)
    w = conn.execute("SELECT status, losses, consec_losses FROM wallets WHERE address=?", (W,)).fetchone()
    assert w["losses"] == 3 and w["status"] == "demoted"


def test_wallet_cap(cfg):
    assert spy.wallet_cap_gate(40.0, 20.0, 1000.0, cfg).state == BLOCK   # 60 > 5% of 1000
    assert spy.wallet_cap_gate(20.0, 20.0, 1000.0, cfg).state == ALLOW


def test_second_leader_buy_in_held_token_is_blocked_and_marked_seen(conn, feed, clock):
    cfg = spy_cfg()
    m, pool = _setup(feed, clock, leader_px=1.0, cur_px=1.02)
    engine.run_cycle(conn, cfg, feed, clock)
    feed.trades[pool].append(gecko_trade(W, "buy", m, 1.01, clock() - 5, "TX1b"))
    clock.advance(300)
    engine.run_cycle(conn, cfg, feed, clock)
    assert conn.execute("SELECT COUNT(*) FROM positions WHERE bot='spy'").fetchone()[0] == 1
    assert conn.execute("SELECT state FROM decisions WHERE bot='spy' AND reason_code='already_holding'").fetchone()[0] == "BLOCK"
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE reason_code='duplicate_proposal'").fetchone()[0] == 0
    assert conn.execute("SELECT outcome FROM seen_events WHERE event_id LIKE 'TX1b:%'").fetchone()[0] == "skip:already_holding"
