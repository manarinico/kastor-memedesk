"""r9: open positions with no executable price are written off at $0 after 4h, in every book."""
from memedesk import db, engine
from conftest import good_token, make_pair


def _opens(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM positions WHERE status='open'")]


def test_open_mints_are_looked_up_directly_every_cycle(conn, cfg, feed, clock):
    m = good_token(feed)
    engine.run_cycle(conn, cfg, feed, clock)
    assert _opens(conn)
    feed.mints.clear()  # token drops out of discovery
    clock.advance(1200)
    seen = []
    orig = feed.dex_tokens
    feed.dex_tokens = lambda mints, kind="dex_tokens": (seen.extend(mints), orig(mints, kind))[1]
    engine.run_cycle(conn, cfg, feed, clock)
    assert m in seen  # still requested by mint, so it stays priced while it trades


def test_drained_pool_written_off_after_4h_not_before(conn, cfg, feed, clock):
    m = good_token(feed)
    engine.run_cycle(conn, cfg, feed, clock)
    books = {p["book"] for p in _opens(conn)}
    assert books >= {"desk", "baseline"}
    t_ok = clock()
    feed.mints.clear()
    feed.pairs[m] = make_pair(m, liq=0.0, now=t_ok)  # pool drained: price shown, no liquidity
    for _ in range(11):  # 11 x 20 min = 3h40m: under 4h, still open
        clock.advance(1200)
        engine.run_cycle(conn, cfg, feed, clock)
    assert {p["book"] for p in _opens(conn)} == books
    del feed.pairs[m]  # then no pair at all
    clock.advance(1200 * 2)  # past 4h since last executable price
    engine.run_cycle(conn, cfg, feed, clock)
    assert _opens(conn) == []
    rows = conn.execute("SELECT book, exit_proceeds_usd, pnl_usd, entry_cost_usd, exit_reason FROM positions").fetchall()
    for r in rows:
        assert r["exit_proceeds_usd"] == 0 and r["pnl_usd"] == -r["entry_cost_usd"]
        assert r["exit_reason"].startswith("unpriced_writeoff")
    d = conn.execute("SELECT details FROM decisions WHERE reason_code='unpriced_writeoff'").fetchall()
    assert len(d) == len(rows)


def test_executable_price_resets_the_clock(conn, cfg, feed, clock):
    m = good_token(feed)
    engine.run_cycle(conn, cfg, feed, clock)
    n = len(_opens(conn))
    feed.mints.clear()
    for i in range(30):  # 10h, liquidity alternates dead/alive every 3h
        clock.advance(1200)
        live = (i // 9) % 2 == 1
        feed.pairs[m] = make_pair(m, liq=100_000.0 if live else 0.0, now=clock())
        engine.run_cycle(conn, cfg, feed, clock)
    assert conn.execute("SELECT COUNT(*) FROM positions WHERE exit_reason LIKE 'unpriced_writeoff%'").fetchone()[0] == 0
