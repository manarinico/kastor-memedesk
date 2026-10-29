"""r6 desk position caps: 4% of current equity per token, 20% total open (Gambler + Spy;
baseline excluded). A breaching entry is skipped and logged with the cap's reason code."""
import json

from memedesk import db, engine, risk
from memedesk.config import load_config
from memedesk.gates import ALLOW, BLOCK, INSUFFICIENT
from conftest import good_token, gecko_trade

W = "LEADERWALLET1111111111111111111111111111111"


def _open(conn, mint, cost, book="desk", bot="gambler", key=None, ts=1_792_900_000.0):
    conn.execute("INSERT INTO positions(proposal_key,book,bot,mint,pair_address,qty,entry_mid,entry_px,"
                 "entry_cost_usd,entry_ts,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (key or f"seed:{book}:{bot}:{mint}", book, bot, mint, f"POOL_{mint}", cost, 1.0, 1.0, cost,
                  ts, "open"))


def _states(gs):
    return {g.name: g.state for g in gs}


def test_cap_gates_pure(cfg):
    pos = [{"mint": "A", "entry_cost_usd": 20.0}, {"mint": "B", "entry_cost_usd": 150.0}]
    # token A: 20 + 20 = 40 = 4% of 1000 -> allowed (at the cap, not over)
    assert _states(risk.exposure_cap_gates(pos, "A", 20.0, 1000.0, cfg)) == {"cap_token_4pct": ALLOW, "cap_total_20pct": ALLOW}
    # token A: 20 + 21 > 40 -> token cap blocks
    assert _states(risk.exposure_cap_gates(pos, "A", 21.0, 1000.0, cfg))["cap_token_4pct"] == BLOCK
    # total: 170 + 31 > 200 -> total cap blocks, token C itself fine
    st = _states(risk.exposure_cap_gates(pos, "C", 31.0, 1000.0, cfg))
    assert st == {"cap_token_4pct": ALLOW, "cap_total_20pct": BLOCK}
    # caps scale with CURRENT equity: same book at equity 800 -> total cap 160 < 170 + 20
    assert _states(risk.exposure_cap_gates(pos, "C", 16.0, 800.0, cfg))["cap_total_20pct"] == BLOCK
    # unknown equity is never permission
    assert set(_states(risk.exposure_cap_gates(pos, "C", 20.0, None, cfg)).values()) == {INSUFFICIENT}


def test_gambler_entry_skipped_by_total_cap(conn, feed, clock, cfg):
    # 4 other desk tokens x $46 = $184 open; equity still $1000 (unpriced opens carried at cost).
    # New 2% entry ($20) -> $204 > $200 -> skip with cap_total_20pct.
    for i in range(4):
        _open(conn, f"OTHER{i}", 46.0, bot="spy" if i % 2 else "gambler")
    good_token(feed)
    engine.run_cycle(conn, cfg, feed, clock)
    assert conn.execute("SELECT COUNT(*) FROM positions WHERE mint='MINTA' AND book='desk'").fetchone()[0] == 0
    r = conn.execute("SELECT state, reason_code, config_version FROM decisions "
                     "WHERE bot='gambler' AND stage='entry' AND mint='MINTA'").fetchone()
    assert (r[0], r[1]) == (BLOCK, "cap_total_20pct")
    assert r[2] == cfg["version_tag"] and r[2].startswith("rules-2026.10.28-r9+")


def test_baseline_book_not_counted_toward_desk_caps(conn, feed, clock, cfg):
    for i in range(4):
        _open(conn, f"OTHER{i}", 100.0, book="baseline", bot="baseline")  # $400 in the benchmark book
    good_token(feed)
    engine.run_cycle(conn, cfg, feed, clock)
    assert conn.execute("SELECT COUNT(*) FROM positions WHERE mint='MINTA' AND book='desk'").fetchone()[0] == 1


def test_spy_entry_skipped_by_token_cap(conn, feed, clock):
    cfg = load_config({"costs": {"latency_s": 0.0}, "spy": {"manual_wallets": [W]},
                       "gambler": {"min_h1_change_pct": 1000.0}})  # gambler off; Spy only
    m = good_token(feed, price=1.02)
    pool = feed.pairs[m]["pairAddress"]
    feed.trending = [(pool, m)]
    feed.trades[pool] = [gecko_trade(W, "buy", m, 1.0, clock() - 120, "TX1")]
    _open(conn, m, 25.0, bot="gambler")  # desk already holds $25 of this token via Gambler
    engine.run_cycle(conn, cfg, feed, clock)  # spy 2% = ~$19.5 -> $44.5 > 4% of ~$975 equity
    assert conn.execute("SELECT COUNT(*) FROM positions WHERE bot='spy'").fetchone()[0] == 0
    r = conn.execute("SELECT state, reason_code, details FROM decisions WHERE bot='spy' AND stage='entry'").fetchone()
    assert (r[0], r[1]) == (BLOCK, "cap_token_4pct")
    gates = {g["name"]: g["state"] for g in json.loads(r[2])["gates"]}
    assert gates["cap_token_4pct"] == BLOCK
    assert conn.execute("SELECT outcome FROM seen_events WHERE event_id LIKE 'TX1:%'").fetchone()[0] == "skip:cap_token_4pct"


def test_entry_within_caps_still_allowed(conn, feed, clock, cfg):
    _open(conn, "OTHER0", 100.0)
    good_token(feed)
    engine.run_cycle(conn, cfg, feed, clock)
    r = conn.execute("SELECT entry_cost_usd FROM positions WHERE mint='MINTA' AND book='desk'").fetchone()
    assert r is not None and abs(r[0] - 20.0) < 1e-9  # standard size unchanged: 2% of equity
