"""One scan/decide/update pass. Deterministic rules only; paper fills only.

Order: discover -> snapshot -> price -> Scout gates -> leader trades -> exits ->
desk breakers -> entries (Gambler, Spy, Baseline) -> equity. Every fetched payload is
persisted as an immutable snapshot BEFORE it is used, and every decision row links to
the snapshot id(s) it used plus the config version.
"""
import datetime as dt
import json
import sqlite3
import statistics
import time
from dataclasses import dataclass

from . import db, fills, gambler, nico, risk, scout, spy
from .feed import DISCOVERY_KINDS
from .gates import ALLOW, BLOCK, INSUFFICIENT, Gate, allow, block, insufficient, combine, num

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    LOCAL_TZ = None


@dataclass
class View:
    mint: str
    pair: dict
    sid: int
    receipt_ts: float

    @property
    def price(self):
        return num(self.pair, "priceUsd")

    @property
    def liq(self):
        return num(self.pair, "liquidity", "usd")

    @property
    def pair_address(self):
        return self.pair.get("pairAddress")

    @property
    def symbol(self):
        return (self.pair.get("baseToken") or {}).get("symbol") or self.mint[:6]


class Ctx:
    def __init__(self, conn, cfg, feed, clock, mode):
        self.conn, self.cfg, self.feed, self.clock, self.mode = conn, cfg, feed, clock, mode
        self.ver = cfg["version_tag"]
        self.seq = 0
        self.fetch_ms = {}
        self.fetch_wall = 0.0
        self.data_ages = []
        self.fill_lags = []
        self.counts = {"entries": 0, "exits": 0, "decisions": 0, "trips": 0}
        self.cycle_id = None
        self.budget_hit = None   # first "fetch budget exhausted" error seen this cycle

    def stale_gate(self, receipt_ts):
        """stale_feed breaker, plus: once the cycle's fetch budget ran out the data is
        incomplete, so the answer is INSUFFICIENT_DATA (never ALLOW), whatever the age."""
        g = risk.stale_feed_breaker(receipt_ts, self.now(), self.cfg)
        if self.budget_hit and g.state == ALLOW:
            return insufficient("stale_feed", f"fetch budget exhausted this cycle; data incomplete ({self.budget_hit})")
        return g

    def now(self):
        return self.clock()

    def fetch(self, fn, *a):
        t0 = time.perf_counter()
        res = fn(*a)
        self.fetch_wall += time.perf_counter() - t0
        lst = res if isinstance(res, list) else [res]
        for r in lst:
            self.snap(r)
            if r.latency_ms is not None:
                self.fetch_ms[r.kind] = self.fetch_ms.get(r.kind, 0) + r.latency_ms
            if not r.ok and (r.error or "").startswith("fetch budget exhausted") and not self.budget_hit:
                self.budget_hit = f"first skipped: {r.kind}"
        return res

    def snap(self, r):
        self.seq += 1
        r.snapshot_id = db.insert_snapshot(self.conn, self.cycle_id, self.seq, r)
        return r.snapshot_id

    def decide(self, book, bot, mint, symbol, stage, gate, snapshot_id=None, aux=None, details=None):
        self.counts["decisions"] += 1
        cur = self.conn.execute(
            "INSERT INTO decisions(cycle_id,ts,book,bot,mint,symbol,stage,state,reason_code,reason,"
            "snapshot_id,aux_snapshot_ids,config_version,details) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.cycle_id, self.now(), book, bot, mint, symbol, stage, gate.state, gate.name,
             gate.reason, snapshot_id, json.dumps(aux or []), self.ver,
             json.dumps(details, default=str) if details is not None else None))
        return cur.lastrowid

    def trip(self, breaker, scope, detail):
        self.counts["trips"] += 1
        self.conn.execute("INSERT INTO breaker_trips(cycle_id,ts,breaker,scope,detail,config_version) "
                          "VALUES(?,?,?,?,?,?)", (self.cycle_id, self.now(), breaker, scope, detail, self.ver))


# ---------------------------------------------------------------- helpers
def _local_day(ts):
    d = dt.datetime.fromtimestamp(ts, LOCAL_TZ) if LOCAL_TZ else dt.datetime.fromtimestamp(ts)
    return d.date().isoformat()


def equity(ctx, book):
    return db.book_state(ctx.conn, book, ctx.cfg["start_book_usd"], ctx.cfg["costs"])


def _fill_inputs(ctx, view):
    """Price used for the paper fill. In replay, apply latency by using the latest saved
    observation known at decision_time + latency (never used for the decision, never later)."""
    lat = ctx.cfg["costs"]["latency_s"]
    now = ctx.now()
    lag = lat + max(0.0, now - view.receipt_ts)
    fut = ctx.feed.mid_at(view.pair_address, now + lat) if lat > 0 else None
    if fut:
        return fut["price"], fut["liq"], fut["sid"], lag
    return view.price, view.liq, view.sid, lag


def open_position(ctx, book, bot, view, budget, proposal_key, decision_id,
                  leader=None, event=None):
    mid, liq, sid, lag = _fill_inputs(ctx, view)
    f = fills.buy(mid, budget, liq, ctx.cfg["costs"])
    if f is None:
        ctx.decide(book, bot, view.mint, view.symbol, "fill",
                   insufficient("fill", "unfillable: price/liquidity missing"), view.sid)
        return None
    try:
        with db.txn(ctx.conn) as c:
            if event is not None:
                c.execute("INSERT INTO seen_events(event_id,cycle_id,ts,wallet,kind,mint,price_usd,block_ts,outcome) "
                          "VALUES(?,?,?,?,?,?,?,?,?)", (event["event_id"], ctx.cycle_id, ctx.now(), event["wallet"],
                                                       event["kind"], event["mint"], event["price_usd"],
                                                       event["block_ts"], "copied"))
            cur = c.execute(
                "INSERT INTO positions(proposal_key,book,bot,mint,symbol,pair_address,dex_id,leader_wallet,"
                "leader_px,leader_ts,qty,entry_mid,entry_px,entry_cost_usd,entry_fee_usd,entry_slip_usd,"
                "entry_ts,entry_snapshot_id,entry_decision_id,entry_liquidity,fill_lag_s,config_version,status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open')",
                (proposal_key, book, bot, view.mint, view.symbol, view.pair_address, view.pair.get("dexId"),
                 (leader or {}).get("wallet"), (leader or {}).get("price_usd"), (leader or {}).get("block_ts"),
                 f["qty"], f["mid"], f["px"], f["cost_usd"], f["fee_usd"], f["slippage_usd"], ctx.now(),
                 sid, decision_id, view.liq, lag, ctx.ver))
            if bot == "spy" and leader:
                c.execute("UPDATE wallets SET copies=copies+1 WHERE address=?", (leader["wallet"],))
    except sqlite3.IntegrityError as e:
        ctx.decide(book, bot, view.mint, view.symbol, "idempotency",
                   block("duplicate_proposal", f"proposal already applied ({e})"), view.sid,
                   details={"proposal_key": proposal_key})
        return None
    ctx.counts["entries"] += 1
    ctx.fill_lags.append(lag)
    db.update_mark(ctx.conn, view.mint, view.pair_address, view.price, view.liq, view.receipt_ts, view.sid)
    return cur.lastrowid


def close_position(ctx, p, view, reason):
    f = fills.guard_sell(fills.sell(view.price, p["qty"], view.liq, ctx.cfg["costs"]), p["entry_mid"])
    if f is None:
        ctx.decide(p["book"], p["bot"], p["mint"], p["symbol"], "exit",
                   insufficient("exit_fill", f"wanted to exit ({reason}) but no fillable price"), view.sid)
        return False
    pnl = f["proceeds_usd"] - p["entry_cost_usd"]
    if f["flagged"]:
        reason = f"{reason} [fill flagged: {f['flagged']}]"
    with db.txn(ctx.conn) as c:
        cur = c.execute(
            "UPDATE positions SET status='closed', exit_ts=?, exit_mid=?, exit_px=?, exit_proceeds_usd=?, "
            "exit_fee_usd=?, exit_slip_usd=?, exit_reason=?, exit_snapshot_id=?, exit_config_version=?, pnl_usd=? "
            "WHERE id=? AND status='open'",
            (ctx.now(), f["mid"], f["px"], f["proceeds_usd"], f["fee_usd"], f["slippage_usd"], reason,
             view.sid, ctx.ver, pnl, p["id"]))
        if cur.rowcount != 1:
            return False
        if p["bot"] == "spy" and p["leader_wallet"]:
            w = c.execute("SELECT * FROM wallets WHERE address=?", (p["leader_wallet"],)).fetchone()
            if w:
                consec = (w["consec_losses"] + 1) if pnl < 0 else 0
                c.execute("UPDATE wallets SET consec_losses=?, wins=wins+?, losses=losses+?, pnl_usd=pnl_usd+? "
                          "WHERE address=?", (consec, int(pnl >= 0), int(pnl < 0), pnl, p["leader_wallet"]))
                if spy.should_demote(consec, ctx.cfg) and w["status"] == "active":
                    c.execute("UPDATE wallets SET status='demoted', demoted_at=?, note=? WHERE address=?",
                              (ctx.now(), f"{consec} losing copies in a row", p["leader_wallet"]))
    ctx.decide(p["book"], p["bot"], p["mint"], p["symbol"], "exit", allow("exit", round(pnl, 4), reason),
               view.sid, details={"position_id": p["id"], "pnl_usd": pnl, "fill": f})
    ctx.counts["exits"] += 1
    return True


def _executable(v):
    return bool(v is not None and v.price and v.liq and v.liq > 0)


def _unpriced_writeoff(ctx, p, v):
    """r9 (Risk Desk, approved by Manari 2026-10-28): an open position with no executable price
    (no pair returned, no price, or pool liquidity <= 0) for longer than risk.unpriced_writeoff_s is
    closed at $0, tagged `unpriced_writeoff`. Missing data counts as a loss, never as a hold at an
    old price. Applies to desk and baseline alike. Every cycle already re-requests each open
    position's mint from DexScreener (direct token lookup), so no separate lookup is needed."""
    if _executable(v):
        return False
    conn = ctx.conn
    m = db.latest_mark(conn, p["mint"], p["pair_address"])
    fallback = max(p["entry_ts"], m["observed_ts"] if (m and (m["liquidity_usd"] or 0) > 0) else 0)
    last_ok = db.kv_get(conn, f"last_exec_ts:{p['id']}", fallback)
    gap = ctx.now() - last_ok
    if gap < ctx.cfg["risk"]["unpriced_writeoff_s"]:
        return False
    lk = conn.execute("SELECT id, ok, error, fetch_ts FROM snapshots WHERE kind='dex_tokens' AND key LIKE ? "
                      "ORDER BY id DESC LIMIT 1", (f"%{p['mint']}%",)).fetchone()
    last_lookup = dict(lk) if lk else None
    reason = (f"unpriced_writeoff: no executable price for {gap / 3600:.1f}h "
              f"(last mark liq {m['liquidity_usd'] if m else None}); valued at $0")
    with db.txn(conn) as c:
        cur = c.execute(
            "UPDATE positions SET status='closed', exit_ts=?, exit_mid=?, exit_px=0, exit_proceeds_usd=0, "
            "exit_fee_usd=0, exit_slip_usd=0, exit_reason=?, exit_snapshot_id=?, exit_config_version=?, pnl_usd=? "
            "WHERE id=? AND status='open'",
            (ctx.now(), m["price_usd"] if m else None, reason, lk["id"] if lk else None, ctx.ver,
             -p["entry_cost_usd"], p["id"]))
        if cur.rowcount != 1:
            return False
    ctx.decide(p["book"], p["bot"], p["mint"], p["symbol"], "exit",
               allow("unpriced_writeoff", round(-p["entry_cost_usd"], 4), reason),
               lk["id"] if lk else None,
               details={"position_id": p["id"], "tag": "unpriced_writeoff", "entry_value_usd": p["entry_cost_usd"],
                        "unpriced_s": gap, "last_executable_ts": last_ok, "last_mark": m, "last_lookup": last_lookup})
    ctx.counts["exits"] += 1
    return True


def _cooldown_gate(ctx, book, bot, mint):
    r = ctx.conn.execute("SELECT MAX(exit_ts) FROM positions WHERE book=? AND bot=? AND mint=? AND status='closed'",
                         (book, bot, mint)).fetchone()[0]
    cd = ctx.cfg["gambler"]["reentry_cooldown_min"] * 60
    if r is not None and ctx.now() - r < cd:
        return block("reentry_cooldown", f"exited {(ctx.now()-r)/60:.0f}m ago")
    return allow("reentry_cooldown")


def _holding_gate(ctx, book, bot, mint):
    """One open position per (book, bot, token). The DB index enforces it too; this gate makes
    the decision log say so instead of logging an ALLOW that the DB then refuses."""
    if ctx.conn.execute("SELECT 1 FROM positions WHERE book=? AND bot=? AND mint=? AND status='open'",
                        (book, bot, mint)).fetchone():
        return block("already_holding", "already holding this token")
    return allow("already_holding")


# ---------------------------------------------------------------- the cycle
def run_cycle(conn, cfg, feed, clock=None, mode="live", source_cycle_id=None, crash_after=None):
    """crash_after: test hook - raise RuntimeError after the named phase."""
    clock = clock or time.time
    ctx = Ctx(conn, cfg, feed, clock, mode)
    t_start = time.perf_counter()
    cyc_started = ctx.now()
    ctx.cycle_id = conn.execute(
        "INSERT INTO cycles(started_at,mode,config_version,status,source_cycle_id) VALUES(?,?,?,?,?)",
        (cyc_started, mode, ctx.ver, "running", source_cycle_id)).lastrowid
    for w in cfg["spy"].get("manual_wallets") or []:
        conn.execute("INSERT OR IGNORE INTO wallets(address,status,source,discovered_at) VALUES(?,?,?,?)",
                     (w, "active", "manual", ctx.now()))

    def checkpoint(name):
        if crash_after == name:
            raise RuntimeError(f"simulated crash after {name}")

    # day-start equity for the daily loss breaker (first cycle of the local day)
    day = _local_day(ctx.now())
    for book in db.BOOKS:
        db.kv_setdefault(conn, f"day_start_equity:{book}:{day}", equity(ctx, book)["equity"])

    # 0. nico side book: seed the ledger position once (idempotent) BEFORE the open-position list
    # is built, so its mint is priced this cycle even if it is not in the scan universe.
    nico.seed(ctx)

    # 1. discovery ------------------------------------------------------------
    disc = ctx.fetch(feed.discover)
    cands, trending = {}, []
    for r in disc:
        if not r.ok:
            continue
        for m, origin in scout.parse_discovery(r.kind, r.payload):
            cands.setdefault(m, origin)
        if r.kind == "gecko_trending":
            trending = scout.gecko_pools(r.payload)
    opens = db.open_positions(conn)
    mints = list(cands) + [p["mint"] for p in opens if p["mint"] not in cands]
    checkpoint("discover")

    # 2. prices ---------------------------------------------------------------
    views, dex_ok_receipts = {}, []
    raw_pairs = {}
    for r in ctx.fetch(feed.dex_tokens, mints) if mints else []:
        if not r.ok or not isinstance(r.payload, list):
            continue
        dex_ok_receipts.append(r.receipt_ts)
        for p in r.payload:
            m = (p.get("baseToken") or {}).get("address") if isinstance(p, dict) else None
            if m:
                raw_pairs.setdefault(m, []).append((p, r))
    for m, lst in raw_pairs.items():
        best = scout.pick_pair([p for p, _ in lst], m)
        if best is not None:
            r = next(rr for p, rr in lst if p is best)
            views[m] = View(m, best, r.snapshot_id, r.receipt_ts)

    def view_for_position(p):
        lst = raw_pairs.get(p["mint"]) or []
        for pair, r in lst:
            if pair.get("pairAddress") == p["pair_address"]:
                return View(p["mint"], pair, r.snapshot_id, r.receipt_ts)
        return None

    for p in opens:
        v = view_for_position(p)
        if v and v.price:
            db.update_mark(conn, p["mint"], p["pair_address"], v.price, v.liq, v.receipt_ts, v.sid)
        if _executable(v):
            db.kv_set(conn, f"last_exec_ts:{p['id']}", v.receipt_ts)
    checkpoint("prices")

    # 3. Scout hard gates ------------------------------------------------------
    approved = {}
    ctx._rug_left = cfg["scout"]["max_rug_checks_per_cycle"]
    pre = []
    for m in cands:
        v = views.get(m)
        g = scout.cheap_gates(v.pair if v else None, v.receipt_ts if v else None, ctx.now(), cfg)
        pre.append((m, v, g))
    # spend the RugCheck budget on the most liquid survivors first
    pre.sort(key=lambda x: -((x[1].liq or 0) if x[1] else 0))
    for m, v, g in pre:
        verdict = combine(g)
        aux = []
        if verdict.state == ALLOW:
            rep, rts, rsid = _rug_report(ctx, m)
            if rsid == "budget":
                g = g + [insufficient("rugcheck_budget", "RugCheck budget for this cycle used up")]
            else:
                if rsid is not None:
                    aux.append(rsid)
                g = g + scout.safety_gates(rep, rts, v.pair, ctx.now(), cfg)
            verdict = combine(g)
        ctx.decide("desk", "scout", m, v.symbol if v else None, "scout", verdict,
                   v.sid if v else None, aux, {"origin": cands[m], "gates": [x.to_dict() for x in g]})
        if verdict.state == ALLOW:
            approved[m] = v
    checkpoint("scout")

    # 4. leader trades (Spy) -------------------------------------------------
    spy_cfg = cfg["spy"]
    disc_pools = trending[:spy_cfg["discovery_pools_per_cycle"]]
    track_pools = [(v.pair_address, m) for m, v in sorted(approved.items(), key=lambda kv: -(kv[1].liq or 0))]
    track_pools = track_pools[:spy_cfg["tracking_pools_per_cycle"]]
    spy_open = db.open_positions(conn, book="desk", bot="spy")
    pools = list(dict.fromkeys([p for p, _ in disc_pools] + [p for p, _ in track_pools] +
                               [p["pair_address"] for p in spy_open]))
    trade_lists, pool_sids = [], {}
    for pool in pools:
        r = ctx.fetch(feed.pool_trades, pool)
        if r.ok:
            trade_lists.append(spy.parse_trades(r.payload))
            pool_sids[pool] = r.snapshot_id
    disc_set = {p for p, _ in disc_pools}
    # wallet discovery from trending pools only
    disc_lists = [spy.parse_trades(_payload_of(ctx, pool_sids[p])) for p in pool_sids if p in disc_set]
    active_n = conn.execute("SELECT COUNT(*) FROM wallets WHERE status='active'").fetchone()[0]
    for c in spy.score_wallets(disc_lists, cfg):
        if active_n >= spy_cfg["max_active_wallets"]:
            break
        cur = conn.execute("INSERT OR IGNORE INTO wallets(address,status,source,discovered_at,score_usd,note) "
                           "VALUES(?,?,?,?,?,?)", (c["wallet"], "active", "gecko_trades_window", ctx.now(),
                                                    c["realized_usd"], f"roi {c['roi']:.0%} over {c['pools']} pool window(s)"))
        active_n += cur.rowcount
    active = {r[0] for r in conn.execute("SELECT address FROM wallets WHERE status='active'")}
    events = sorted([e for lst in trade_lists for e in lst], key=lambda e: (e["block_ts"], e["tx"]))
    events = list({e["event_id"]: e for e in events}.values())  # duplicate events across pools
    events.sort(key=lambda e: (e["block_ts"], e["tx"]))
    checkpoint("trades")

    # 5. exits ----------------------------------------------------------------
    blocked_mints = set()
    for p in db.open_positions(conn):
        v = view_for_position(p)
        if p["book"] in db.SIDE_BOOKS:
            # isolated side book: its own mark log + stop-only exit; no desk breaker applies
            nico.on_cycle(ctx, p, v, close_position)
            continue
        if _unpriced_writeoff(ctx, p, v):
            continue
        if v is None or v.price is None:
            reason = gambler.exit_reason(p, None, ctx.now(), cfg, p["bot"])
            ctx.decide(p["book"], p["bot"], p["mint"], p["symbol"], "exit",
                       insufficient("exit_price", f"no current price; holding" + (f" (wanted {reason})" if reason else "")),
                       None)
            continue
        if p["book"] == "desk":
            g = risk.liquidity_drop_breaker(p["entry_liquidity"], v.liq, cfg)
            if g.state == BLOCK:
                ctx.trip("liquidity_drop", p["mint"], g.reason)
                blocked_mints.add(p["mint"])
                close_position(ctx, p, v, f"liquidity_drop_breaker: {g.reason}")
                continue
        reason = None
        if p["bot"] == "spy":
            sells = [e for e in events if e["wallet"] == p["leader_wallet"] and e["mint"] == p["mint"]
                     and e["kind"] == "sell" and e["block_ts"] > (p["leader_ts"] or 0)]
            if sells:
                reason = f"mirror_exit: leader sold at {dt.datetime.fromtimestamp(sells[0]["block_ts"], dt.timezone.utc):%H:%M:%S}Z"
                for e in sells:
                    conn.execute("INSERT OR IGNORE INTO seen_events(event_id,cycle_id,ts,wallet,kind,mint,price_usd,block_ts,outcome) "
                                 "VALUES(?,?,?,?,?,?,?,?,?)", (e["event_id"], ctx.cycle_id, ctx.now(), e["wallet"], e["kind"],
                                                              e["mint"], e["price_usd"], e["block_ts"], "mirror_exit"))
        reason = reason or gambler.exit_reason(p, v.price, ctx.now(), cfg, p["bot"])
        if reason:
            close_position(ctx, p, v, reason)
    checkpoint("exits")

    # 6. desk breakers ----------------------------------------------------------
    eq = equity(ctx, "desk")
    day_start = db.kv_get(conn, f"day_start_equity:desk:{day}")
    recent = conn.execute("SELECT pnl_usd, exit_ts FROM positions WHERE book='desk' AND status='closed' "
                          "ORDER BY exit_ts DESC, id DESC LIMIT 50").fetchall()
    breakers = [
        risk.daily_loss_breaker(eq["equity"], day_start, cfg),
        risk.consecutive_loss_breaker([r[0] for r in recent], recent[0][1] if recent else None, ctx.now(), cfg),
        ctx.stale_gate(max(dex_ok_receipts) if dex_ok_receipts else None),
    ]
    for g in breakers:
        if g.state != ALLOW:
            ctx.trip(g.name, "desk", f"{g.state}: {g.reason}")
    desk_gate = combine(breakers)
    desk_gate = Gate("desk_breakers", desk_gate.state, desk_gate.reason, desk_gate.value)

    # 6b. re-price entry candidates right before entries (the RugCheck and trade fetches
    # above take a while; we don't act on a price from a minute ago). Candidates are the
    # Scout-approved tokens plus tokens whose earlier price already passes the momentum
    # signal (so the no-filter baseline is judged on equally fresh data).
    refresh = {m: (approved.get(m) or views[m]) for m in views
               if m in approved or (m in cands and gambler.signal(views[m].pair, cfg).state == ALLOW)}
    if refresh:
        fresh = {}
        for r in ctx.fetch(feed.dex_tokens, list(refresh), "dex_tokens_refresh"):
            if not r.ok or not isinstance(r.payload, list):
                continue
            for m in refresh:
                p = scout.pick_pair([x for x in r.payload if isinstance(x, dict)], m, refresh[m].pair_address)
                if p is not None and m not in fresh:
                    fresh[m] = View(m, p, r.snapshot_id, r.receipt_ts)
        for m, v in fresh.items():
            if m in approved:
                approved[m] = v
            views[m] = v

    # 7a. Gambler entries ----------------------------------------------------
    n_open = len(db.open_positions(conn, book="desk", bot="gambler"))
    for m, v in approved.items():
        gates = [desk_gate]
        stale = ctx.stale_gate(v.receipt_ts)
        ctx.data_ages.append(ctx.now() - v.receipt_ts)
        recheck = [x for x in scout.cheap_gates(v.pair, v.receipt_ts, ctx.now(), cfg) if x.name != "snapshot_fresh"]
        gates += [stale] + recheck + [gambler.signal(v.pair, cfg), _cooldown_gate(ctx, "desk", "gambler", m),
                  _holding_gate(ctx, "desk", "gambler", m)]
        if m in blocked_mints:
            gates.append(block("liquidity_drop", "token tripped liquidity breaker this cycle"))
        if n_open >= cfg["gambler"]["max_open"]:
            gates.append(block("max_open", f"{n_open} open >= {cfg['gambler']['max_open']}"))
        eqd = equity(ctx, "desk")["equity"]
        size = gambler.position_size(eqd, cfg, "gambler")
        gates += risk.exposure_cap_gates(db.open_positions(conn, book="desk"), m, size, eqd, cfg)
        g = combine(gates)
        did = ctx.decide("desk", "gambler", m, v.symbol, "entry", g, v.sid,
                         details={"gates": [x.to_dict() for x in gates], "size_usd": size})
        if g.state == ALLOW:
            key = f"gambler:{m}:{db.payload_sha(v.pair)[:16]}"
            if open_position(ctx, "desk", "gambler", v, size, key, did):
                n_open += 1
    checkpoint("gambler")

    # 7b. Spy entries ----------------------------------------------------------
    for e in events:
        if e["kind"] != "buy" or e["wallet"] not in active:
            continue
        if conn.execute("SELECT 1 FROM seen_events WHERE event_id=?", (e["event_id"],)).fetchone():
            continue  # duplicate leader event: already handled in an earlier cycle
        lag_s = ctx.now() - e["block_ts"]
        v = views.get(e["mint"])
        later_sell = any(x["wallet"] == e["wallet"] and x["mint"] == e["mint"] and x["kind"] == "sell"
                         and x["block_ts"] > e["block_ts"] for x in events)
        gates = [desk_gate]
        if lag_s > spy_cfg["max_signal_age_min"] * 60:
            gates.append(block("signal_age", f"leader buy {lag_s/60:.0f}m old"))
        if later_sell:
            gates.append(block("leader_exited", "leader already sold"))
        if e["mint"] not in approved:
            gates.append(block("scout_not_approved", "token not approved by Scout this cycle")
                         if v else insufficient("scout_not_approved", "no pair data for token"))
        gates.append(_holding_gate(ctx, "desk", "spy", e["mint"]))
        fill = None
        if v is not None:
            gates.append(ctx.stale_gate(v.receipt_ts))
        g = combine(gates)
        eqd = equity(ctx, "desk")["equity"]
        size = gambler.position_size(eqd, cfg, "spy")
        if g.state == ALLOW:
            mid, liq, _, _ = _fill_inputs(ctx, v)
            fill = fills.buy(mid, size, liq, cfg["costs"])
            chase = spy.chase_gate(fill["px"] if fill else None, e["price_usd"], cfg)
            wopen = sum(p["entry_cost_usd"] for p in db.open_positions(conn, "desk", "spy") if p["leader_wallet"] == e["wallet"])
            gates += [chase, spy.wallet_cap_gate(wopen, size, eqd, cfg)]
            gates += risk.exposure_cap_gates(db.open_positions(conn, book="desk"), e["mint"], size, eqd, cfg)
            g = combine(gates)
        det = {"wallet": e["wallet"], "leader_px": e["price_usd"], "lag_s": round(lag_s, 1), "tx": e["tx"],
               "our_fill_px": fill["px"] if fill else None, "gates": [x.to_dict() for x in gates]}
        did = ctx.decide("desk", "spy", e["mint"], v.symbol if v else None, "entry", g, v.sid if v else None,
                         [pool_sids.get(v.pair_address)] if v else [], det)
        if g.state == ALLOW:
            open_position(ctx, "desk", "spy", v, size, f"spy:{e['event_id']}", did, leader=e, event=e)
        else:
            conn.execute("INSERT OR IGNORE INTO seen_events(event_id,cycle_id,ts,wallet,kind,mint,price_usd,block_ts,outcome) "
                         "VALUES(?,?,?,?,?,?,?,?,?)", (e["event_id"], ctx.cycle_id, ctx.now(), e["wallet"], e["kind"],
                                                      e["mint"], e["price_usd"], e["block_ts"], f"skip:{g.name}"))
    checkpoint("spy")

    # 7c. Baseline (counterfactual: same signal, no Scout safety gates, no desk breakers)
    if cfg["baseline"]["enabled"]:
        nb = len(db.open_positions(conn, book="baseline"))
        for m, v in views.items():
            if m not in cands:
                continue
            gates = [ctx.stale_gate(v.receipt_ts)]
            liq = v.liq
            gates.append(insufficient("fill_liquidity", "liquidity missing") if liq is None else
                         (block("fill_liquidity", f"liq ${liq:,.0f} < floor") if liq < cfg["baseline"]["min_liquidity_usd"]
                          else allow("fill_liquidity", liq)))
            gates += [gambler.signal(v.pair, cfg), _cooldown_gate(ctx, "baseline", "baseline", m),
                      _holding_gate(ctx, "baseline", "baseline", m)]
            if nb >= cfg["baseline"]["max_open"]:
                gates.append(block("max_open", "baseline max open"))
            g = combine(gates)
            if g.state != ALLOW:
                continue  # baseline rejections aren't logged row-by-row (it's only a yardstick)
            size = gambler.position_size(equity(ctx, "baseline")["equity"], cfg, "gambler")
            did = ctx.decide("baseline", "baseline", m, v.symbol, "entry", g, v.sid, details={"size_usd": size})
            if open_position(ctx, "baseline", "baseline", v, size, f"baseline:{m}:{db.payload_sha(v.pair)[:16]}", did):
                nb += 1

    # 8. equity + timings ------------------------------------------------------
    side = [b for b in db.SIDE_BOOKS
            if conn.execute("SELECT 1 FROM positions WHERE book=? LIMIT 1", (b,)).fetchone()]
    for book in list(db.BOOKS) + side:
        start = cfg["start_book_usd"] if book in db.BOOKS else cfg[book]["start_book_usd"]
        s = db.book_state(conn, book, start, cfg["costs"])
        conn.execute("INSERT INTO equity_history(cycle_id,ts,book,equity,cash,open_value,n_open,n_unpriced) "
                     "VALUES(?,?,?,?,?,?,?,?)", (ctx.cycle_id, ctx.now(), book, s["equity"], s["cash"],
                                                 s["open_value"], s["n_open"], s["n_unpriced"]))
    total_s = time.perf_counter() - t_start
    timings = {
        "total_s": round(total_s, 2),
        "fetch_wall_s": round(ctx.fetch_wall, 2),
        "decision_s": round(total_s - ctx.fetch_wall, 3),
        "fetch_ms_by_kind": {k: round(v) for k, v in ctx.fetch_ms.items()},
        "data_age_at_entry_check_s": _stats(ctx.data_ages),
        "simulated_fill_lag_s": _stats(ctx.fill_lags),
        "configured_latency_s": cfg["costs"]["latency_s"],
    }
    summary = {"candidates": len(cands), "priced": len(views), "approved": len(approved),
               "desk_breakers": [x.to_dict() for x in breakers], **ctx.counts,
               "desk_equity": round(equity(ctx, "desk")["equity"], 2),
               "baseline_equity": round(equity(ctx, "baseline")["equity"], 2),
               "active_wallets": len(active),
               "sources_failed": [f"{r.kind}: {r.error}" for r in disc if not r.ok],
               "fetch_budget_hit": ctx.budget_hit}
    ns = nico.book_summary(conn, cfg)
    if ns:
        lo = [x["last"] for x in ns["positions"] if x["last"] and x["last"]["cycle_id"] == ctx.cycle_id]
        summary["nico"] = {"equity": round(ns["equity"], 2), "start_usd": ns["start_usd"], "n_open": ns["n_open"],
                           "this_cycle": [{k: (round(l[k], 4) if isinstance(l[k], float) and k != "observed_px" else l[k]) for k in ("event", "observed_px", "value_usd", "unrealized_usd",
                                                             "to_stop_pct", "gap_s", "missed_slots")} for l in lo]}
    conn.execute("UPDATE cycles SET ended_at=?, status='done', timings=?, summary=? WHERE id=?",
                 (ctx.now(), json.dumps(timings), json.dumps(summary), ctx.cycle_id))
    return {"cycle_id": ctx.cycle_id, "timings": timings, "summary": summary}


def _stats(xs):
    if not xs:
        return None
    return {"n": len(xs), "median": round(statistics.median(xs), 2), "max": round(max(xs), 2)}


def _payload_of(ctx, sid):
    r = ctx.conn.execute("SELECT payload FROM snapshots WHERE id=?", (sid,)).fetchone()
    return json.loads(r[0]) if r and r[0] else None


def _rug_report(ctx, mint):
    """Reuse a recent saved RugCheck report (by snapshot) or fetch one if budget allows.
    -> (report|None, receipt_ts|None, snapshot_id|None|'budget')"""
    maxage = ctx.cfg["scout"]["rug_report_max_age_s"]
    r = ctx.conn.execute("SELECT id, payload, receipt_ts FROM snapshots WHERE kind='rug_report' AND key=? AND ok=1 "
                         "AND receipt_ts <= ? ORDER BY receipt_ts DESC LIMIT 1", (mint, ctx.now())).fetchone()
    if r and ctx.now() - r["receipt_ts"] <= maxage:
        return json.loads(r["payload"]), r["receipt_ts"], r["id"]
    if ctx._rug_left <= 0:
        return None, None, "budget"
    ctx._rug_left -= 1
    res = ctx.fetch(ctx.feed.rug_report, mint)
    if not res.ok:
        return None, None, res.snapshot_id
    return res.payload, res.receipt_ts, res.snapshot_id
