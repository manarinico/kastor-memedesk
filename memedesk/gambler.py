"""Gambler: directional momentum entries on Scout-approved coins.

Signal (all must hold, from the DexScreener pair snapshot):
  * 1h price change between +min_h1 and +max_h1 percent (trend, but not a vertical candle)
  * 5m price change >= min_m5 (still moving up right now)
  * 1h buys / sells >= ratio (more buyers than sellers)
  * 1h volume >= min USD
Size = risk_pct x CURRENT book equity. Never a function of the last trade.
Exits: take-profit, stop-loss, time stop (checked every cycle on the latest mark).
"""
from .gates import allow, block, insufficient, num


def position_size(equity_usd, cfg, bot="gambler"):
    """The ONLY sizing rule: fixed fraction of current equity."""
    if equity_usd is None or equity_usd <= 0:
        return 0.0
    return cfg[bot]["risk_pct"] * equity_usd


def signal(pair, cfg):
    s = cfg["gambler"]
    h1 = num(pair, "priceChange", "h1")
    m5 = num(pair, "priceChange", "m5")
    buys = num(pair, "txns", "h1", "buys")
    sells = num(pair, "txns", "h1", "sells")
    vol = num(pair, "volume", "h1")
    if None in (h1, m5, buys, sells, vol):
        return insufficient("momentum", "momentum fields missing")
    ratio = buys / sells if sells > 0 else (float("inf") if buys > 0 else 0.0)
    v = {"h1": h1, "m5": m5, "buy_sell": round(ratio, 2) if ratio != float("inf") else "inf", "vol_h1": vol}
    if h1 < s["min_h1_change_pct"]:
        return block("momentum", f"1h change {h1:+.1f}% < +{s['min_h1_change_pct']:.0f}%", v)
    if h1 > s["max_h1_change_pct"]:
        return block("momentum", f"1h change {h1:+.1f}% > +{s['max_h1_change_pct']:.0f}% (chasing)", v)
    if m5 < s["min_m5_change_pct"]:
        return block("momentum", f"5m change {m5:+.1f}% < {s['min_m5_change_pct']:+.0f}%", v)
    if ratio < s["min_buy_sell_ratio_h1"]:
        return block("momentum", f"1h buy/sell {ratio:.2f} < {s['min_buy_sell_ratio_h1']}", v)
    if vol < s["min_h1_volume_usd"]:
        return block("momentum", f"1h volume ${vol:,.0f} < ${s['min_h1_volume_usd']:,.0f}", v)
    return allow("momentum", v)


def exit_reason(position, mid, now, cfg, bot="gambler"):
    """-> reason string or None. mid=None means no price: only the time stop can be
    *noticed*, but we cannot fill without a price, so caller holds."""
    s = cfg[bot] if bot in ("gambler", "spy") else cfg["gambler"]
    held_min = (now - position["entry_ts"]) / 60
    if mid is not None and position["entry_px"]:
        chg = (mid / position["entry_px"] - 1) * 100
        if "take_profit_pct" in s and chg >= s["take_profit_pct"]:
            return f"take_profit {chg:+.1f}%"
        if chg <= -s["stop_loss_pct"]:
            return f"stop_loss {chg:+.1f}%"
    if held_min >= s["time_stop_min"]:
        return f"time_stop {held_min:.0f}m"
    return None
