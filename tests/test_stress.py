"""Stress: stale snapshots, missing fields, duplicate events, out-of-order updates,
API timeouts, restart mid-run, idempotent proposals."""
import pytest
import requests

from memedesk import db, engine
from memedesk.config import load_config
from memedesk.feed import LiveFeed
from memedesk.gates import INSUFFICIENT
from conftest import FakeFeed, good_token, make_pair


def n_pos(conn, **kw):
    q = "SELECT COUNT(*) FROM positions WHERE 1=1" + "".join(f" AND {k}='{v}'" for k, v in kw.items())
    return conn.execute(q).fetchone()[0]


def test_stale_snapshot_never_trades(conn, cfg, feed, clock):
    good_token(feed)
    feed.receipt_lag = cfg["risk"]["stale_feed_s"] + 30
    engine.run_cycle(conn, cfg, feed, clock)
    assert n_pos(conn) == 0
    st = conn.execute("SELECT state FROM decisions WHERE bot='scout'").fetchone()[0]
    assert st == INSUFFICIENT


@pytest.mark.parametrize("field", ["priceChange", "txns", "volume", "liquidity", "pairCreatedAt"])
def test_missing_fields_never_trade(conn, cfg, feed, clock, field):
    m = good_token(feed)
    feed.drop_fields[m] = [field]
    engine.run_cycle(conn, cfg, feed, clock)
    assert n_pos(conn, book="desk") == 0
    states = {r[0] for r in conn.execute("SELECT state FROM decisions WHERE mint=? AND book='desk'", (m,))}
    assert "INSUFFICIENT_DATA" in states


def test_api_timeouts_everywhere(conn, cfg, feed, clock):
    good_token(feed)
    feed.fail = {"all"}
    res = engine.run_cycle(conn, cfg, feed, clock)
    assert n_pos(conn) == 0
    assert conn.execute("SELECT status FROM cycles").fetchone()[0] == "done"
    assert conn.execute("SELECT COUNT(*) FROM snapshots WHERE ok=0").fetchone()[0] >= 1
    assert res["summary"]["sources_failed"]


def test_rugcheck_timeout_is_insufficient(conn, cfg, feed, clock):
    good_token(feed)
    feed.fail = {"rug_report"}
    engine.run_cycle(conn, cfg, feed, clock)
    assert n_pos(conn, book="desk") == 0
    assert conn.execute("SELECT state FROM decisions WHERE bot='scout'").fetchone()[0] == INSUFFICIENT


def test_livefeed_turns_timeout_into_failed_result(cfg):
    class Boom:
        headers = {}
        def get(self, *a, **k):
            raise requests.Timeout("read timed out")
    f = LiveFeed(cfg, session=Boom())
    r = f.rug_report("X")
    assert not r.ok and "Timeout" in r.error and r.receipt_ts is not None


def test_out_of_order_marks_are_ignored(conn):
    assert db.update_mark(conn, "M", "P", 2.0, 1e5, 200.0, 1)
    assert not db.update_mark(conn, "M", "P", 1.0, 1e5, 100.0, 2)   # older observation arrives late
    assert db.latest_mark(conn, "M", "P")["price_usd"] == 2.0
    assert not db.update_mark(conn, "M", "P", 3.0, 1e5, 200.0, 3)   # duplicate timestamp
    assert db.latest_mark(conn, "M", "P")["price_usd"] == 2.0


def test_duplicate_proposal_is_idempotent(conn, cfg, feed, clock):
    m = good_token(feed)
    engine.run_cycle(conn, cfg, feed, clock)
    assert n_pos(conn, book="desk", bot="gambler") == 1
    # replaying the exact same proposal key must not create a second position
    ctx = engine.Ctx(conn, cfg, feed, clock, "test"); ctx.cycle_id = 99
    p = feed.pairs[m]
    v = engine.View(m, p, 1, clock())
    key = conn.execute("SELECT proposal_key FROM positions WHERE bot='gambler'").fetchone()[0]
    assert engine.open_position(ctx, "desk", "gambler", v, 20.0, key, None) is None
    assert n_pos(conn, book="desk", bot="gambler") == 1
    # same data next cycle -> no second open position in same token either
    engine.run_cycle(conn, cfg, feed, clock)
    assert n_pos(conn, book="desk", bot="gambler") == 1


@pytest.mark.parametrize("phase", ["discover", "prices", "scout", "trades", "exits", "gambler", "spy"])
def test_restart_mid_run_no_duplicates_no_double_pnl(tmp_path, cfg, clock, phase):
    path = str(tmp_path / "r.sqlite")
    conn = db.connect(path)
    feed = FakeFeed(clock)
    m = good_token(feed)
    engine.run_cycle(conn, cfg, feed, clock)                       # opens a gambler position
    clock.advance(600)
    feed.pairs[m] = make_pair(m, price=1.5, now=clock() - 600)      # +50% -> take profit
    with pytest.raises(RuntimeError):
        engine.run_cycle(conn, cfg, feed, clock, crash_after=phase)
    conn.close()
    conn = db.connect(path)                                         # "restart"
    engine.run_cycle(conn, cfg, feed, clock)
    engine.run_cycle(conn, cfg, feed, clock)                        # and once more
    rows = conn.execute("SELECT status, pnl_usd FROM positions WHERE book='desk' AND bot='gambler'").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "closed"
    s = db.book_state(conn, "desk", cfg["start_book_usd"])
    assert abs(s["equity"] - (cfg["start_book_usd"] + rows[0]["pnl_usd"])) < 1e-9
    assert conn.execute("SELECT COUNT(*) FROM cycles WHERE status='running'").fetchone()[0] == 1


def test_held_token_is_not_re_proposed(conn, cfg, feed, clock):
    good_token(feed)
    engine.run_cycle(conn, cfg, feed, clock)
    clock.advance(600)
    engine.run_cycle(conn, cfg, feed, clock)
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE reason_code='duplicate_proposal'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE reason_code='already_holding' AND book='desk'").fetchone()[0] == 1
