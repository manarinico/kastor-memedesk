"""NICO-PICK v1.0: the "Nico pick" side bet as a third, isolated PAPER book (`nico`).

* One off-code position, booked by Gambler from cycle 39 real data and approved by Manari.
  It is seeded ONCE from a committed, sha256-pinned copy of the ledger
  (memedesk/seeds/nico_pick_ledger.json). Seeding is idempotent: the proposal key is unique,
  so re-running never inserts a second copy. Entry time / price / qty / cost come from the
  ledger verbatim; nothing is re-filled.
* The book NEVER opens positions on its own. This module only seeds and manages exits.
* Each cycle the engine already prices every open position's mint, even if the token left
  the scan universe (engine step 2 adds open mints to the price fetch), so the pick is marked
  with that cycle's DexScreener observation for its pair.
* Exit rule: stop only (no take-profit, no time stop unless the ledger states one).
  The stop is a TRIGGER on the observed mid (<= stop_trigger_px); the fill is the desk's
  paper sell model (fees + slippage + impact, fill guard) at the first EXECUTABLE observation
  at or after the trigger (gap rule, STANDARDS s3). trigger_px, observed px and modeled fill
  px are logged separately in side_book_log. A trigger with no fillable price stays pending
  and fills at the next executable observation, whatever its price.
* Separate $500 line: not in desk equity, desk exposure caps, breakers, or the $1,000 scorecard.
  Every cycle writes one side_book_log row (mark / no_price / stop_*) with the gap since the
  previous row and the implied missed 20-min slots, so missed cycles are explicit.
PAPER ONLY: no wallet, no key, no order adapter.
"""
import datetime as dt
import hashlib
import json
import os

from . import db, fills
from .gates import allow, insufficient

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = None

BOOK = "nico"
BOT = "nico"
HERE = os.path.dirname(os.path.abspath(__file__))


def seed_path(cfg):
    p = cfg["nico"]["seed_file"]
    return p if os.path.isabs(p) else os.path.join(HERE, p)


def _et_ts(s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET).timestamp()


def load_seed(cfg):
    """-> (ledger dict, sha256) ; raises ValueError if the file doesn't match the pinned hash."""
    with open(seed_path(cfg), "rb") as f:
        raw = f.read()
    sha = hashlib.sha256(raw).hexdigest()
    want = cfg["nico"].get("seed_sha256")
    if want and sha != want:
        raise ValueError(f"seed ledger sha256 {sha[:12]} != pinned {want[:12]}")
    return json.loads(raw), sha


def proposal_key(led):
    return f"nico:seed:{led['mint']}:{led['entry_ts_et']}"


def _log(ctx, p, event, **kw):
    cols = ["cycle_id", "ts", "book", "position_id", "mint", "event"] + list(kw)
    vals = [ctx.cycle_id, ctx.now(), BOOK, p["id"] if p else None, p["mint"] if p else None, event]
    vals += [json.dumps(v, default=str) if k == "details" else v for k, v in kw.items()]
    ctx.conn.execute(f"INSERT INTO side_book_log({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                     vals)


def seed(ctx):
    """Insert the ledger position once. Live mode only, and only once the clock has reached the
    ledger's entry time (a replay over older cycles never sees it). Returns position id or None."""
    ncfg = ctx.cfg.get("nico") or {}
    if not ncfg.get("enabled") or ctx.mode != "live":
        return None
    try:
        led, sha = load_seed(ctx.cfg)
    except (OSError, ValueError) as e:
        ctx.decide(BOOK, BOT, None, None, "seed", insufficient("nico_seed", f"seed ledger unusable: {e}"))
        return None
    key = proposal_key(led)
    if ctx.conn.execute("SELECT 1 FROM positions WHERE proposal_key=?", (key,)).fetchone():
        return None  # already seeded (idempotent)
    entry_ts = _et_ts(led["entry_ts_et"])
    if ctx.now() < entry_ts:
        return None
    link = ncfg.get("seed_link") or {}
    qty, px, mid = float(led["qty"]), float(led["entry_px"]), float(led["entry_mid"])
    fee = float(led.get("fee_usd") or 0) + float(led.get("network_usd") or 0)
    with db.txn(ctx.conn) as c:
        if c.execute("SELECT 1 FROM positions WHERE proposal_key=?", (key,)).fetchone():
            return None
        did = ctx.decide(BOOK, BOT, led["mint"], led.get("symbol"), "seed",
                         allow("nico_seed", led["cost_usd"], "seeded from committed ledger (off-code pick; no new fill)"),
                         link.get("entry_snapshot_id"), details={"ledger": led, "ledger_sha256": sha, "link": link})
        cur = c.execute(
            "INSERT INTO positions(proposal_key,book,bot,mint,symbol,pair_address,dex_id,qty,entry_mid,entry_px,"
            "entry_cost_usd,entry_fee_usd,entry_slip_usd,entry_ts,entry_snapshot_id,entry_decision_id,"
            "entry_liquidity,fill_lag_s,config_version,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open') "
            "ON CONFLICT(proposal_key) DO NOTHING",
            (key, BOOK, BOT, led["mint"], led.get("symbol"), link.get("pair_address"), link.get("dex_id"), qty, mid, px,
             float(led["cost_usd"]), fee, qty * (px - mid), entry_ts, link.get("entry_snapshot_id"), did,
             (led.get("signal") or {}).get("liq_usd"), None, ctx.ver))
        if cur.rowcount != 1:
            return None
        pid = cur.lastrowid
        tstop = led.get("time_stop_min")
        c.execute("INSERT INTO side_positions(position_id,book,stop_trigger_px,take_profit_px,time_stop_ts,stop_rule,"
                  "source,ledger_sha256,seeded_cycle_id,seeded_at,config_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (pid, BOOK, float(led["stop_trigger_px"]), led.get("take_profit_px"),
                   entry_ts + float(tstop) * 60 if tstop else None, led.get("stop_rule"),
                   led.get("source"), sha, ctx.cycle_id, ctx.now(), ctx.ver))
        _log(ctx, {"id": pid, "mint": led["mint"]}, "seed", observed_px=mid, observed_ts=entry_ts,
             stop_trigger_px=float(led["stop_trigger_px"]), config_version=ctx.ver,
             details={"entry_px": px, "qty": qty, "cost_usd": led["cost_usd"], "entry_ts_et": led["entry_ts_et"]})
    return pid


def _gap(ctx, p):
    r = ctx.conn.execute("SELECT MAX(ts) FROM side_book_log WHERE position_id=? AND event!='seed'", (p["id"],)).fetchone()[0]
    prev = r if r is not None else p["entry_ts"]
    gap = ctx.now() - prev
    slot = float(ctx.cfg["nico"].get("schedule_slot_s", 1200))
    return gap, max(0, round(gap / slot) - 1)


def _side(ctx, p):
    r = ctx.conn.execute("SELECT * FROM side_positions WHERE position_id=?", (p["id"],)).fetchone()
    return dict(r) if r else {}


def on_cycle(ctx, p, v, close_fn):
    """Mark + stop check for one open nico position. `v` is this cycle's View for its pair
    (None if the cycle got no price). `close_fn` is engine.close_position."""
    sp = _side(ctx, p)
    stop = sp.get("stop_trigger_px")
    tp = sp.get("take_profit_px")
    tstop = sp.get("time_stop_ts")
    gap, missed = _gap(ctx, p)
    pend_key = f"nico_stop_pending:{p['id']}"
    pending = db.kv_get(ctx.conn, pend_key)
    base = {"gap_s": gap, "missed_slots": missed, "stop_trigger_px": stop, "config_version": ctx.ver}
    if v is None or v.price is None:
        ctx.decide(BOOK, BOT, p["mint"], p["symbol"], "exit",
                   insufficient("exit_price", "no current price this cycle; holding, stop not checked"
                                + (" (stop pending)" if pending else "")))
        _log(ctx, p, "no_price", details={"pending": pending}, **base)
        return None
    sell = fills.guard_sell(fills.sell(v.price, p["qty"], v.liq, ctx.cfg["costs"]), p["entry_mid"])
    value = sell["proceeds_usd"] if sell else None
    to_stop = (1 - stop / v.price) * 100 if stop else None  # % fall from this mark to the trigger
    liq_chg = (v.liq / p["entry_liquidity"] - 1) * 100 if v.liq and p["entry_liquidity"] else None
    obs = {"observed_px": v.price, "observed_ts": v.receipt_ts, "liquidity_usd": v.liq, "snapshot_id": v.sid,
           "value_usd": value, "gross_value_usd": p["qty"] * v.price,
           "unrealized_usd": (value - p["entry_cost_usd"]) if value is not None else None,
           "to_stop_pct": to_stop, "fill_flag": sell["flagged"] if sell else None}
    det = {"chg_vs_entry_px_pct": (v.price / p["entry_px"] - 1) * 100, "liq_vs_entry_pct": liq_chg,
           "modeled_exit_px_now": sell["px"] if sell else None}
    reason = None
    if pending or (stop and v.price <= stop):
        reason = (f"stop_loss trigger {stop:.8g} (-20% vs entry px {p['entry_px']:.8g}); first executable "
                  f"observed px {v.price:.8g} ({det['chg_vs_entry_px_pct']:+.1f}% vs entry px)")
    elif tp is not None and v.price >= tp:
        reason = f"take_profit trigger {tp:.8g}; observed px {v.price:.8g}"
    elif tstop is not None and ctx.now() >= tstop:
        reason = "time_stop (per ledger)"
    if reason is None:
        _log(ctx, p, "mark", details=det, **base, **obs)
        return None
    if close_fn(ctx, p, v, reason):
        row = ctx.conn.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
        ctx.conn.execute("DELETE FROM kv WHERE k=?", (pend_key,))
        _log(ctx, p, "stop_filled" if reason.startswith("stop") else "exit_filled",
             trigger_px=stop if reason.startswith("stop") else tp, modeled_fill_px=row["exit_px"],
             details={**det, "trigger": pending or {"cycle_id": ctx.cycle_id, "observed_px": v.price},
                      "exit_mid": row["exit_mid"], "proceeds_usd": row["exit_proceeds_usd"],
                      "pnl_usd": row["pnl_usd"], "reason": reason},
             **base, **{**obs, "fill_flag": sell["flagged"] if sell else None})
        return reason
    if reason.startswith("stop"):
        db.kv_set(ctx.conn, pend_key, pending or {"cycle_id": ctx.cycle_id, "observed_px": v.price, "ts": ctx.now()})
        _log(ctx, p, "stop_pending_unfillable", trigger_px=stop, details=det, **base, **obs)
    return None


def book_summary(conn, cfg):
    """Separate line for the report / cycle summary. None if the book has no positions."""
    ncfg = cfg.get("nico") or {}
    if not conn.execute("SELECT 1 FROM positions WHERE book=?", (BOOK,)).fetchone():
        return None
    s = db.book_state(conn, BOOK, ncfg.get("start_book_usd", 500.0), cfg["costs"])
    pos = [dict(r) for r in conn.execute("SELECT * FROM positions WHERE book=? ORDER BY id", (BOOK,))]
    out = {**s, "start_usd": ncfg.get("start_book_usd", 500.0), "positions": []}
    for p in pos:
        last = conn.execute("SELECT * FROM side_book_log WHERE position_id=? AND event!='seed' ORDER BY id DESC LIMIT 1",
                            (p["id"],)).fetchone()
        last_obs = conn.execute("SELECT * FROM side_book_log WHERE position_id=? AND observed_px IS NOT NULL "
                                "AND event!='seed' ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
        agg = conn.execute("SELECT COUNT(*), SUM(event='no_price'), COALESCE(SUM(missed_slots),0), MAX(gap_s) "
                           "FROM side_book_log WHERE position_id=? AND event!='seed'", (p["id"],)).fetchone()
        sp = conn.execute("SELECT * FROM side_positions WHERE position_id=?", (p["id"],)).fetchone()
        out["positions"].append({"position": p, "side": dict(sp) if sp else {},
                                 "last": dict(last) if last else None, "last_obs": dict(last_obs) if last_obs else None,
                                 "cycles_logged": agg[0], "no_price_cycles": agg[1] or 0,
                                 "missed_slots": agg[2], "max_gap_s": agg[3]})
    return out
