"""Risk Desk: desk-level circuit breakers. Pure functions -> Gate.

BLOCK = breaker tripped. INSUFFICIENT_DATA = cannot prove it is safe (also no entries).
"""
from .gates import allow, block, insufficient


def daily_loss_breaker(equity_now, day_start_equity, cfg):
    name = "daily_loss"
    if equity_now is None or not day_start_equity:
        return insufficient(name, "no day-start equity")
    chg = (equity_now / day_start_equity - 1) * 100
    if chg <= -cfg["risk"]["daily_loss_pct"]:
        return block(name, f"day P&L {chg:.2f}% <= -{cfg['risk']['daily_loss_pct']}%", round(chg, 3))
    return allow(name, round(chg, 3))


def consecutive_loss_breaker(recent_pnls_newest_first, last_exit_ts, now, cfg):
    """Trips when the last N closed desk trades were all losses and the last one
    closed less than cooldown minutes ago."""
    name = "consecutive_losses"
    n = cfg["risk"]["max_consecutive_losses"]
    streak = 0
    for p in recent_pnls_newest_first:
        if p < 0:
            streak += 1
        else:
            break
    if streak >= n and last_exit_ts is not None:
        mins = (now - last_exit_ts) / 60
        if mins < cfg["risk"]["consecutive_cooldown_min"]:
            return block(name, f"{streak} losses in a row; cooling down ({mins:.0f}/{cfg['risk']['consecutive_cooldown_min']:.0f} min)", streak)
    return allow(name, streak)


def liquidity_drop_breaker(entry_liquidity, current_liquidity, cfg):
    """Per position. BLOCK => force exit and no new entries in that token."""
    name = "liquidity_drop"
    if not entry_liquidity or current_liquidity is None:
        return insufficient(name, "liquidity unknown")
    drop = (1 - current_liquidity / entry_liquidity) * 100
    if drop >= cfg["risk"]["liquidity_drop_pct"]:
        return block(name, f"liquidity fell {drop:.1f}% since entry", round(drop, 2))
    return allow(name, round(drop, 2))


def stale_feed_breaker(receipt_ts, now, cfg, limit_s=None):
    name = "stale_feed"
    limit = cfg["risk"]["stale_feed_s"] if limit_s is None else limit_s
    if receipt_ts is None:
        return insufficient(name, "no market data received")
    age = now - receipt_ts
    if age > limit:
        return block(name, f"data is {age:.0f}s old > {limit:.0f}s", round(age, 1))
    if age < -5:
        return insufficient(name, f"data timestamp {-age:.0f}s in the future", round(age, 1))
    return allow(name, round(age, 1))


def exposure_cap_gates(open_desk_positions, mint, new_cost, equity_now, cfg):
    """r6 pre-entry caps. open_desk_positions: open DESK rows (Gambler + Spy; never baseline).
    Exposure is cost basis (entry_cost_usd). -> [token gate, total gate]. A breach is BLOCK
    (the entry is skipped, not resized); unknown equity is INSUFFICIENT_DATA."""
    r = cfg["risk"]
    tok_name, tot_name = "cap_token_4pct", "cap_total_20pct"
    if equity_now is None or equity_now <= 0 or new_cost is None:
        return [insufficient(tok_name, "desk equity unknown"), insufficient(tot_name, "desk equity unknown")]
    tok_open = sum(p["entry_cost_usd"] or 0.0 for p in open_desk_positions if p["mint"] == mint)
    tot_open = sum(p["entry_cost_usd"] or 0.0 for p in open_desk_positions)
    tok_cap = r["max_token_pct"] / 100.0 * equity_now
    tot_cap = r["max_total_open_pct"] / 100.0 * equity_now
    out = []
    v = {"open_usd": round(tok_open, 4), "new_usd": round(new_cost, 4), "cap_usd": round(tok_cap, 4)}
    if tok_open + new_cost > tok_cap + 1e-9:
        out.append(block(tok_name, f"token exposure ${tok_open + new_cost:,.2f} > {r['max_token_pct']:g}% cap ${tok_cap:,.2f}", v))
    else:
        out.append(allow(tok_name, v))
    v = {"open_usd": round(tot_open, 4), "new_usd": round(new_cost, 4), "cap_usd": round(tot_cap, 4)}
    if tot_open + new_cost > tot_cap + 1e-9:
        out.append(block(tot_name, f"total open exposure ${tot_open + new_cost:,.2f} > {r['max_total_open_pct']:g}% cap ${tot_cap:,.2f}", v))
    else:
        out.append(allow(tot_name, v))
    return out
