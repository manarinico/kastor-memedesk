"""r8 NICO-PICK v1.0: isolated `nico` side book (paper only)."""
import json

import pytest

from memedesk import db, engine, fills, nico, report, risk
from conftest import FakeClock, FakeFeed, good_token, make_pair

LED = json.load(open(nico.seed_path({"nico": {"seed_file": "seeds/nico_pick_ledger.json"}})))
MINT = LED["mint"]
PAIR = "EFX34rwn4GTMfcq9HsNajcMma311UHJEZXBvSUiaYEaU"
ENTRY_TS = nico._et_ts(LED["entry_ts_et"])


@pytest.fixture
def clock():
    return FakeClock(ENTRY_TS + 20 * 60)  # first cycle after the pick


@pytest.fixture
def feed(clock):
    return FakeFeed(clock)


def price_nico(feed, price, liq=77494.11, scan=False):
    """Make the feed price the pick's pair. scan=False: NOT in discovery (out of the scan universe)."""
    feed.pairs[MINT] = make_pair(MINT, pair_addr=PAIR, price=price, liq=liq, h1=-30.0, m5=-5.0,
                                 now=feed.clock(), symbol="Fartcoin")
    if scan and MINT not in feed.mints:
        feed.mints.append(MINT)


def nico_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM positions WHERE book='nico'")]


def log(conn, event=None):
    q = "SELECT * FROM side_book_log" + (" WHERE event=?" if event else "") + " ORDER BY id"
    return [dict(r) for r in conn.execute(q, (event,) if event else ())]


# ---------------------------------------------------------------- seeding
def test_seed_matches_ledger_exactly_and_is_idempotent(conn, feed, clock, cfg):
    price_nico(feed, 0.00082)
    engine.run_cycle(conn, cfg, feed, clock)
    clock.advance(1200)
    engine.run_cycle(conn, cfg, feed, clock)
    ctx = engine.Ctx(conn, cfg, feed, clock, "live")
    ctx.cycle_id = 99
    assert nico.seed(ctx) is None  # direct re-seed: no-op
    rows = nico_rows(conn)
    assert len(rows) == 1
    p = rows[0]
    assert p["bot"] == "nico" and p["mint"] == MINT and p["pair_address"] == PAIR
    assert p["entry_ts"] == ENTRY_TS  # original entry time preserved (13:10:46 ET), not the seeding cycle's time
    assert p["entry_px"] == LED["entry_px"] and p["entry_mid"] == LED["entry_mid"] and p["qty"] == LED["qty"]
    assert p["entry_cost_usd"] == 500.0 and p["entry_fee_usd"] == pytest.approx(1.55)
    assert p["entry_px"] * p["qty"] + p["entry_fee_usd"] == pytest.approx(500.0, abs=0.01)  # cost reconciles
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE book='nico' AND stage='seed'").fetchone()[0] == 1
    sp = dict(conn.execute("SELECT * FROM side_positions").fetchone())
    assert sp["stop_trigger_px"] == LED["stop_trigger_px"] and sp["take_profit_px"] is None and sp["time_stop_ts"] is None
    assert sp["stop_trigger_px"] == pytest.approx(0.8 * LED["entry_px"])
    assert len(log(conn, "seed")) == 1 and len(log(conn, "mark")) == 2


def test_no_seed_before_entry_time_or_in_replay(conn, feed, cfg):
    early = FakeClock(ENTRY_TS - 60)
    feed.clock = early
    engine.run_cycle(conn, cfg, feed, early)
    late = FakeClock(ENTRY_TS + 600)
    feed.clock = late
    engine.run_cycle(conn, cfg, feed, late, mode="replay")
    assert nico_rows(conn) == []


def test_tampered_seed_is_refused(conn, feed, clock, cfg):
    cfg = {**cfg, "nico": {**cfg["nico"], "seed_sha256": "0" * 64}}
    engine.run_cycle(conn, cfg, feed, clock)
    assert nico_rows(conn) == []
    r = conn.execute("SELECT state, reason_code FROM decisions WHERE book='nico' AND stage='seed'").fetchone()
    assert tuple(r) == ("INSUFFICIENT_DATA", "nico_seed")


# ---------------------------------------------------------------- separation
def test_separate_from_desk_equity_exposure_breakers(conn, feed, clock, cfg):
    price_nico(feed, 0.00070)  # pick down ~16%: nico book loses, desk must not notice
    good_token(feed)           # a normal Gambler entry on another token
    res = engine.run_cycle(conn, cfg, feed, clock)
    # desk book: only the Gambler entry; equity ~ $1,000 less its own entry costs, nothing from the $500 pick
    desk = db.book_state(conn, "desk", cfg["start_book_usd"], cfg["costs"])
    assert desk["n_open"] == 1 and desk["equity"] > 995
    assert db.book_state(conn, "baseline", 1000.0, cfg["costs"])["equity"] > 995
    nb = db.book_state(conn, "nico", 500.0, cfg["costs"])
    assert nb["n_open"] == 1 and nb["equity"] < 430
    assert res["summary"]["desk_equity"] == pytest.approx(desk["equity"], abs=0.01)
    assert res["summary"]["nico"]["equity"] == pytest.approx(nb["equity"], abs=0.01)
    # $500 open would be 50% of desk equity: if it counted, the 20% total cap would block this Gambler entry
    assert conn.execute("SELECT COUNT(*) FROM positions WHERE book='desk' AND bot='gambler'").fetchone()[0] == 1
    gates = risk.exposure_cap_gates(db.open_positions(conn, book="desk"), MINT, 20.0, desk["equity"], cfg)
    assert {g.state for g in gates} == {"ALLOW"}
    assert conn.execute("SELECT COUNT(*) FROM breaker_trips").fetchone()[0] == 0
    eh = {r[0]: r[1] for r in conn.execute("SELECT book, equity FROM equity_history")}
    assert set(eh) == {"desk", "baseline", "nico"} and eh["desk"] == pytest.approx(desk["equity"])
    # report: separate block, desk/baseline books unchanged
    d = report.compute(conn, cfg)
    assert set(d["books"]) == {"desk", "baseline"} and "nico" not in d["bots"]
    txt = report.render(d, cfg)
    assert "NICO PICK (separate $500.00 paper line; NOT in desk equity" in txt


def test_nico_never_opens_new_positions(conn, feed, clock, cfg):
    price_nico(feed, 0.00083, scan=True)
    feed.pairs[MINT]["priceChange"] = {"m5": 2.0, "h1": 20.0}  # momentum signal fires on the same mint
    good_token(feed)
    for _ in range(3):
        engine.run_cycle(conn, cfg, feed, clock)
        clock.advance(1200)
    assert len(nico_rows(conn)) == 1
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE book='nico' AND stage='entry'").fetchone()[0] == 0


# ---------------------------------------------------------------- stop: trigger vs fill
def test_stop_trigger_and_fill_accounting(conn, feed, clock, cfg):
    price_nico(feed, 0.00072)  # above the 0.00066538 trigger: hold
    engine.run_cycle(conn, cfg, feed, clock)
    assert nico_rows(conn)[0]["status"] == "open"
    clock.advance(3 * 1200)    # two 20-min slots missed, then a gap through the stop
    gap_px, liq = 0.00060, 60000.0
    price_nico(feed, gap_px, liq=liq)
    engine.run_cycle(conn, cfg, feed, clock)
    p = nico_rows(conn)[0]
    assert p["status"] == "closed"
    f = fills.sell(gap_px, LED["qty"], liq, cfg["costs"])
    assert p["exit_mid"] == pytest.approx(gap_px)                 # first observed price, not the trigger level
    assert p["exit_px"] == pytest.approx(f["px"]) and p["exit_px"] < gap_px < LED["stop_trigger_px"]
    assert p["exit_proceeds_usd"] == pytest.approx(f["proceeds_usd"])
    assert p["pnl_usd"] == pytest.approx(f["proceeds_usd"] - 500.0)
    ev = log(conn, "stop_filled")[0]
    assert ev["trigger_px"] == LED["stop_trigger_px"] and ev["observed_px"] == gap_px
    assert ev["modeled_fill_px"] == pytest.approx(f["px"]) and ev["missed_slots"] == 2
    # cash reconciles: start - cost + proceeds
    assert db.book_state(conn, "nico", 500.0, cfg["costs"])["cash"] == pytest.approx(f["proceeds_usd"])
    # desk untouched by the nico loss
    assert db.book_state(conn, "desk", 1000.0, cfg["costs"])["equity"] == pytest.approx(1000.0)


def test_triggered_stop_with_no_fill_stays_pending_then_fills_next_executable(conn, feed, clock, cfg):
    price_nico(feed, 0.00060, liq=None)  # triggers, but no liquidity -> cannot model the sell
    engine.run_cycle(conn, cfg, feed, clock)
    assert nico_rows(conn)[0]["status"] == "open" and len(log(conn, "stop_pending_unfillable")) == 1
    clock.advance(1200)
    price_nico(feed, 0.00070)  # recovered above the trigger, but the stop already fired: fill here
    engine.run_cycle(conn, cfg, feed, clock)
    p = nico_rows(conn)[0]
    assert p["status"] == "closed" and p["exit_mid"] == pytest.approx(0.00070)
    assert conn.execute("SELECT COUNT(*) FROM kv WHERE k LIKE 'nico_stop_pending:%'").fetchone()[0] == 0


def test_no_take_profit_or_time_stop(conn, feed, clock, cfg):
    price_nico(feed, 0.0020)        # +140%: no TP, so hold
    engine.run_cycle(conn, cfg, feed, clock)
    clock.advance(24 * 3600)        # a day later: no time stop either
    engine.run_cycle(conn, cfg, feed, clock)
    assert nico_rows(conn)[0]["status"] == "open"


# ---------------------------------------------------------------- marks outside the scan universe
def test_marked_when_out_of_scan_universe_and_missed_cycles_logged(conn, feed, clock, cfg):
    price_nico(feed, 0.00075)       # not in feed.mints: discovery never returns it
    engine.run_cycle(conn, cfg, feed, clock)
    m = log(conn, "mark")[-1]
    assert m["observed_px"] == pytest.approx(0.00075) and m["cycle_id"] == 1
    assert "dex_tokens" in feed.calls
    mk = db.latest_mark(conn, MINT, PAIR)
    assert mk["price_usd"] == pytest.approx(0.00075)
    exp = fills.guard_sell(fills.sell(0.00075, LED["qty"], 77494.11, cfg["costs"]), LED["entry_mid"])["proceeds_usd"]
    assert m["value_usd"] == pytest.approx(exp) and m["unrealized_usd"] == pytest.approx(exp - 500.0)
    assert m["to_stop_pct"] == pytest.approx((1 - LED["stop_trigger_px"] / 0.00075) * 100)
    # a cycle with no price: logged honestly, position held, stop not checked
    del feed.pairs[MINT]
    clock.advance(1200)
    engine.run_cycle(conn, cfg, feed, clock)
    np_ = log(conn, "no_price")
    assert len(np_) == 1 and nico_rows(conn)[0]["status"] == "open"
    # a 1h gap -> 2 missed slots on the next mark
    clock.advance(3600)
    price_nico(feed, 0.00076)
    engine.run_cycle(conn, cfg, feed, clock)
    assert log(conn, "mark")[-1]["missed_slots"] == 2
    s = nico.book_summary(conn, cfg)["positions"][0]
    assert s["no_price_cycles"] == 1 and s["missed_slots"] == 2 and s["cycles_logged"] == 3
