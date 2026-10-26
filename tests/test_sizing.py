from memedesk import db, engine, gambler
from conftest import good_token, make_pair


def test_size_is_two_percent_of_current_equity(cfg):
    assert gambler.position_size(1000.0, cfg) == 20.0
    assert gambler.position_size(850.0, cfg) == 17.0
    assert gambler.position_size(0.0, cfg) == 0.0
    assert gambler.position_size(None, cfg) == 0.0


def _seed_closed(conn, pnl, clock):
    conn.execute("INSERT INTO positions(proposal_key,book,bot,mint,qty,entry_mid,entry_px,entry_cost_usd,entry_ts,"
                 "status,exit_ts,exit_proceeds_usd,pnl_usd) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (f"seed{pnl}", "desk", "gambler", "SEED", 1, 1, 1, 100.0, clock() - 9000, "closed",
                  clock() - 8000, 100.0 + pnl, pnl))


def _entry_cost(conn):
    return conn.execute("SELECT entry_cost_usd FROM positions WHERE mint='MINTA'").fetchone()[0]


def test_engine_size_follows_equity_not_last_trade(tmp_path, cfg, clock):
    from conftest import FakeFeed
    # book A: last trade lost $100; book B: last trade won $100. Different equity -> sizes 2% of each.
    sizes = {}
    for name, pnl in (("lost", -100.0), ("won", 100.0)):
        conn = db.connect(str(tmp_path / f"{name}.sqlite"))
        feed = FakeFeed(clock)
        good_token(feed)
        _seed_closed(conn, pnl, clock)
        engine.run_cycle(conn, cfg, feed, clock)
        sizes[name] = _entry_cost(conn)
    assert abs(sizes["lost"] - 0.02 * 900) < 1e-9
    assert abs(sizes["won"] - 0.02 * 1100) < 1e-9


def test_same_equity_same_size_regardless_of_last_outcome(tmp_path, cfg, clock):
    from conftest import FakeFeed
    sizes = []
    for name, pnls in (("a", [-50.0, 50.0]), ("b", [50.0, -50.0])):  # same equity, opposite last trade
        conn = db.connect(str(tmp_path / f"{name}.sqlite"))
        for i, p in enumerate(pnls):
            conn.execute("INSERT INTO positions(proposal_key,book,bot,mint,qty,entry_mid,entry_px,entry_cost_usd,entry_ts,"
                         "status,exit_ts,exit_proceeds_usd,pnl_usd) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (f"s{i}", "desk", "gambler", f"S{i}", 1, 1, 1, 100.0, clock() - 9000, "closed",
                          clock() - 8000 + i, 100.0 + p, p))
        feed = FakeFeed(clock)
        good_token(feed)
        engine.run_cycle(conn, cfg, feed, clock)
        sizes.append(_entry_cost(conn))
    assert sizes[0] == sizes[1] == 20.0
