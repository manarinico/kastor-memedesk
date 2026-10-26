"""Plain-English scorecard."""
import datetime as dt
import json
import statistics

from . import db, nico

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    TZ = None


def _fmt_ts(ts):
    return dt.datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M %Z") if ts else "-"


def max_drawdown(equities):
    peak, mdd = None, 0.0
    for e in equities:
        peak = e if peak is None else max(peak, e)
        if peak:
            mdd = max(mdd, (peak - e) / peak)
    return mdd * 100


def bot_stats(conn, book, bot=None, costs=None):
    q, a = "SELECT * FROM positions WHERE book=?", [book]
    if bot:
        q += " AND bot=?"; a.append(bot)
    rows = [dict(r) for r in conn.execute(q, a)]
    closed = [r for r in rows if r["status"] == "closed"]
    opens = [r for r in rows if r["status"] == "open"]
    unreal = 0.0
    for p in opens:
        v, _ = db.position_value(conn, p, costs)
        unreal += v - p["entry_cost_usd"]
    pnls = sorted([r["pnl_usd"] for r in closed], key=abs, reverse=True)
    wins = sum(1 for p in pnls if p >= 0)
    hold = [(r["exit_ts"] - r["entry_ts"]) / 3600 for r in closed]
    costs = sum((r["entry_fee_usd"] or 0) + (r["entry_slip_usd"] or 0) + (r["exit_fee_usd"] or 0) + (r["exit_slip_usd"] or 0)
                for r in closed) + sum((r["entry_fee_usd"] or 0) + (r["entry_slip_usd"] or 0) for r in opens)
    turnover = sum(r["entry_cost_usd"] for r in rows) + sum(r["exit_proceeds_usd"] or 0 for r in closed)
    gross_abs = sum(abs(p) for p in pnls)
    realized = sum(pnls)
    return {
        "trades_closed": len(closed), "open": len(opens), "wins": wins,
        "win_rate": (wins / len(closed) * 100) if closed else None,
        "realized_usd": realized, "unrealized_usd": unreal, "total_usd": realized + unreal,
        "costs_usd": costs, "turnover_usd": turnover,
        "hold_h_median": statistics.median(hold) if hold else None,
        "hold_h_mean": statistics.mean(hold) if hold else None,
        "top3_share_pct": (sum(abs(p) for p in pnls[:3]) / gross_abs * 100) if gross_abs else None,
        "realized_ex_best_usd": (realized - max(pnls)) if pnls else None,
        "exit_reasons": _count([(r["exit_reason"] or "").split(" ")[0].split(":")[0] for r in closed]),
    }


def _count(xs):
    d = {}
    for x in xs:
        d[x] = d.get(x, 0) + 1
    return dict(sorted(d.items(), key=lambda kv: -kv[1]))


def compute(conn, cfg):
    out = {"books": {}, "bots": {}}
    for book in db.BOOKS:
        s = db.book_state(conn, book, cfg["start_book_usd"], cfg["costs"])
        eqs = [r[0] for r in conn.execute("SELECT equity FROM equity_history WHERE book=? ORDER BY id", (book,))]
        s["max_drawdown_pct"] = max_drawdown([cfg["start_book_usd"]] + eqs)
        s["return_pct"] = (s["equity"] / cfg["start_book_usd"] - 1) * 100
        out["books"][book] = s
    for book, bot in (("desk", "gambler"), ("desk", "spy"), ("baseline", "baseline")):
        out["bots"][bot] = bot_stats(conn, book, bot, cfg["costs"])
    out["desk_all"] = bot_stats(conn, "desk", costs=cfg["costs"])
    out["nico"] = nico.book_summary(conn, cfg)  # isolated side book; never in desk/baseline numbers
    out["cycles"] = conn.execute("SELECT COUNT(*), MIN(started_at), MAX(started_at), "
                                 "SUM(status='running') FROM cycles").fetchone()
    out["trips"] = [dict(r) for r in conn.execute(
        "SELECT breaker, COUNT(*) n, MAX(ts) last, MAX(detail) example FROM breaker_trips GROUP BY breaker ORDER BY n DESC")]
    sc = conn.execute("SELECT state, COUNT(*), COUNT(DISTINCT mint) FROM decisions WHERE stage='scout' GROUP BY state").fetchall()
    out["scout_states"] = {r[0]: {"evaluations": r[1], "tokens": r[2]} for r in sc}
    out["top_rejections"] = [dict(r) for r in conn.execute(
        "SELECT reason_code, state, COUNT(*) n, COUNT(DISTINCT mint) tokens, MAX(reason) example FROM decisions "
        "WHERE stage='scout' AND state!='ALLOW' GROUP BY reason_code, state ORDER BY n DESC LIMIT 8")]
    out["entry_states"] = {f"{r[0]}:{r[1]}": r[2] for r in conn.execute(
        "SELECT bot, state, COUNT(*) FROM decisions WHERE stage='entry' AND book='desk' GROUP BY bot, state")}
    out["entry_block_reasons"] = [dict(r) for r in conn.execute(
        "SELECT bot, reason_code, state, COUNT(*) n FROM decisions WHERE stage='entry' AND book='desk' AND state!='ALLOW' "
        "GROUP BY bot, reason_code, state ORDER BY n DESC LIMIT 8")]
    out["wallets"] = _count([r[0] for r in conn.execute("SELECT status FROM wallets")])
    out["wallet_rows"] = [dict(r) for r in conn.execute(
        "SELECT address, status, source, score_usd, copies, wins, losses, consec_losses, pnl_usd FROM wallets "
        "ORDER BY copies DESC, score_usd DESC LIMIT 8")]
    out["chase_skips"] = []
    for r in conn.execute("SELECT ts, mint, symbol, details FROM decisions WHERE bot='spy' AND reason_code='max_chase' "
                          "AND state='BLOCK' ORDER BY id DESC LIMIT 5"):
        d = json.loads(r["details"])
        out["chase_skips"].append({"ts": r["ts"], "symbol": r["symbol"], "wallet": d.get("wallet"),
                                   "lag_s": d.get("lag_s"), "leader_px": d.get("leader_px"), "fill_px": d.get("our_fill_px")})
    t = [json.loads(r[0]) for r in conn.execute("SELECT timings FROM cycles WHERE status='done' AND timings IS NOT NULL")]
    out["timing"] = {
        "cycles": len(t),
        "total_s_median": statistics.median([x["total_s"] for x in t]) if t else None,
        "fetch_s_median": statistics.median([x["fetch_wall_s"] for x in t]) if t else None,
        "decision_s_median": statistics.median([x["decision_s"] for x in t]) if t else None,
        "last": t[-1] if t else None,
    }
    out["versions"] = [r[0] for r in conn.execute("SELECT DISTINCT config_version FROM decisions")]
    out["snapshots"] = conn.execute("SELECT COUNT(*), SUM(ok=0) FROM snapshots").fetchone()
    return out


def _m(x):
    return "n/a" if x is None else f"${x:,.2f}"


def _p(x):
    return "n/a" if x is None else f"{x:.1f}%"


def _hold(h):
    if h is None:
        return "n/a"
    return f"{h * 60:.0f}m" if h < 1 else f"{h:.1f}h"


def render(d, cfg, title="MEMEDESK PAPER SCORECARD (paper money only)"):
    L = [title, "=" * len(title)]
    n, t0, t1, running = d["cycles"]
    L.append(f"Cycles run: {n} ({_fmt_ts(t0)} -> {_fmt_ts(t1)})" + (f"; {running} interrupted" if running else ""))
    L.append(f"Rules version(s): {', '.join(d['versions']) or cfg['version_tag']}")
    L.append(f"Snapshots stored: {d['snapshots'][0]} ({d['snapshots'][1] or 0} failed fetches kept as evidence)")
    L.append("")
    for book, s in d["books"].items():
        label = "DESK (Gambler + Spy)" if book == "desk" else "BASELINE (same momentum rule, no safety filters)"
        L.append(f"{label}: book {_m(s['equity'])} from {_m(cfg['start_book_usd'])} ({s['return_pct']:+.2f}%), "
                 f"cash {_m(s['cash'])}, {s['n_open']} open"
                 + (f" ({s['n_unpriced']} without a fresh price)" if s["n_unpriced"] else "")
                 + f", max drawdown {s['max_drawdown_pct']:.2f}%")
    L += render_nico(d.get("nico"))
    L.append("")
    L.append("By bot:")
    for bot, s in d["bots"].items():
        L.append(f"  {bot:9s} closed {s['trades_closed']:3d} | open {s['open']} | win rate {_p(s['win_rate'])} | "
                 f"realized {_m(s['realized_usd'])} | unrealized {_m(s['unrealized_usd'])} | costs {_m(s['costs_usd'])} | "
                 f"turnover {_m(s['turnover_usd'])} | median hold {_hold(s['hold_h_median'])} (mean {_hold(s.get('hold_h_mean'))})")
        if s["exit_reasons"]:
            L.append(f"            exits: {s['exit_reasons']}")
        if bot == "baseline" and s.get("top3_share_pct") is not None:
            L.append(f"            concentration: top 3 trades = {s['top3_share_pct']:.0f}% of absolute realized P&L; "
                     f"realized without best trade = {_m(s['realized_ex_best_usd'])}")
    da = d["desk_all"]
    if da["top3_share_pct"] is not None:
        L.append(f"  Concentration: top 3 desk trades = {da['top3_share_pct']:.0f}% of absolute realized P&L; "
                 f"realized without the best trade = {_m(da['realized_ex_best_usd'])}"
                 + ("  <- a few trades explain most of the P&L" if da["top3_share_pct"] > 60 else ""))
    g, b = d["bots"]["gambler"], d["bots"]["baseline"]
    L.append("")
    L.append("Do the filters add anything? Gambler (with Scout + breakers) vs Baseline (no filters):")
    L.append(f"  trades {g['trades_closed']}+{g['open']} open vs {b['trades_closed']}+{b['open']} open; "
             f"P&L {_m(g['total_usd'])} vs {_m(b['total_usd'])}; win rate {_p(g['win_rate'])} vs {_p(b['win_rate'])}; "
             f"desk DD {d['books']['desk']['max_drawdown_pct']:.2f}% vs baseline DD {d['books']['baseline']['max_drawdown_pct']:.2f}%")
    tot = g["trades_closed"] + b["trades_closed"]
    if tot < 30:
        L.append(f"  Verdict: not enough closed trades yet ({tot}) to say - need ~30+ each before trusting this.")
    L.append("")
    L.append("Circuit breaker trips: " + (", ".join(f"{t['breaker']} x{t['n']}" for t in d["trips"]) or "none"))
    for t in d["trips"][:4]:
        L.append(f"  {t['breaker']}: last {_fmt_ts(t['last'])} e.g. {t['example']}")
    ss = d["scout_states"]
    tot_ev = sum(v["evaluations"] for v in ss.values())
    if tot_ev:
        L.append("")
        L.append("Scout: " + ", ".join(f"{k} {v['evaluations']} ({v['evaluations']/tot_ev*100:.0f}%, {v['tokens']} tokens)" for k, v in ss.items()))
        L.append("Top rejection reasons:")
        for r in d["top_rejections"]:
            L.append(f"  {r['reason_code']:24s} {r['state']:17s} x{r['n']:4d} ({r['tokens']} tokens)  e.g. {r['example']}")
    if d["entry_block_reasons"]:
        L.append("Entry skips (desk): " + "; ".join(f"{r['bot']}/{r['reason_code']} {r['state']} x{r['n']}" for r in d["entry_block_reasons"]))
    L.append("")
    L.append(f"Spy wallets: {d['wallets'] or 'none yet'}")
    for w in d["wallet_rows"][:5]:
        L.append(f"  {w['address'][:10]}… {w['status']:8s} src={w['source']} window-realized={_m(w['score_usd'])} "
                 f"copies={w['copies']} W/L={w['wins']}/{w['losses']} streak-losses={w['consec_losses']}")
    for c in d["chase_skips"]:
        L.append(f"  max-chase skip: {c['symbol']} wallet {c['wallet'][:10]}… lag {c['lag_s']}s leader ${c['leader_px']:.8g} vs our fill ${c['fill_px']:.8g}")
    tm = d["timing"]
    if tm["cycles"]:
        L.append("")
        L.append(f"Timing: median cycle {tm['total_s_median']}s (fetch {tm['fetch_s_median']}s, decide {tm['decision_s_median']}s)")
        last = tm["last"]
        L.append(f"  last cycle: data age at entry check {last['data_age_at_entry_check_s']}, simulated fill lag {last['simulated_fill_lag_s']}")
    return "\n".join(L)


def render_nico(n):
    """Separate block for the isolated `nico` side book (NICO-PICK v1.0)."""
    if not n:
        return []
    L = [f"NICO PICK (separate {_m(n['start_usd'])} paper line; NOT in desk equity, exposure, breakers or the "
         f"$1,000 scorecard): book {_m(n['equity'])} ({(n['equity'] / n['start_usd'] - 1) * 100:+.2f}%), "
         f"cash {_m(n['cash'])}, {n['n_open']} open"]
    for x in n["positions"]:
        p, sp, lo = x["position"], x["side"], x["last_obs"]
        head = (f"  {p['symbol']} ({p['mint'][:6]}…) {p['status']}: entry {p['entry_px']:.8g} at {_fmt_ts(p['entry_ts'])}, "
                f"cost {_m(p['entry_cost_usd'])}, stop trigger {sp.get('stop_trigger_px') or 0:.8g}, "
                f"TP {sp.get('take_profit_px') or 'none'}, time stop {'per ledger' if sp.get('time_stop_ts') else 'none'}")
        L.append(head)
        if p["status"] == "closed":
            L.append(f"    closed {_fmt_ts(p['exit_ts'])}: observed {p['exit_mid']:.8g} -> modeled fill {p['exit_px']:.8g}, "
                     f"proceeds {_m(p['exit_proceeds_usd'])}, P&L {_m(p['pnl_usd'])} ({p['exit_reason']})")
        elif lo:
            L.append(f"    mark {lo['observed_px']:.8g} (cycle {lo['cycle_id']}, {_fmt_ts(lo['observed_ts'])}), "
                     f"liquidation value {_m(lo['value_usd'])}, unrealized {_m(lo['unrealized_usd'])}, "
                     f"price must fall {lo['to_stop_pct']:.1f}% more to trigger the stop")
        else:
            L.append("    no mark yet")
        L.append(f"    cycles logged {x['cycles_logged']}, without a price {x['no_price_cycles']}, "
                 f"implied missed 20-min slots {x['missed_slots']}, longest gap "
                 + (f"{x['max_gap_s'] / 60:.0f} min" if x["max_gap_s"] else "n/a"))
    return L
