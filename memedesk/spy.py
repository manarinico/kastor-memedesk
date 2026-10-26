"""Spy: copy a small set of wallets that look profitable in free public trade data.

Discovery (free): GeckoTerminal /pools/{pool}/trades returns the last ~300 swaps with the
signer wallet (tx_from_address) and USD value. For the top trending pools we compute,
per wallet, realized USD = sells - buys over that window, keeping only wallets whose
FIRST trade in the window is a buy (so the sell proceeds come from a buy we saw).
This is weak evidence (one short window, no full history) - see README.
Tracking: the same trades endpoint on watched pools; leader buys/sells are matched by
wallet address and deduplicated by tx hash.
"""
import datetime as dt
from .gates import allow, block, insufficient, num
from .scout import STABLES


def _ts(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def parse_trades(payload):
    """-> list of normalized trade dicts sorted by block time (out-of-order safe)."""
    out = []
    for t in ((payload or {}).get("data") or []) if isinstance(payload, dict) else []:
        a = (t or {}).get("attributes") or {}
        kind = a.get("kind")
        if kind == "buy":
            mint, px = a.get("to_token_address"), num(a, "price_to_in_usd")
        elif kind == "sell":
            mint, px = a.get("from_token_address"), num(a, "price_from_in_usd")
        else:
            continue
        ts = _ts(a.get("block_timestamp"))
        w = a.get("tx_from_address")
        if not (mint and w and ts and a.get("tx_hash")) or mint in STABLES or px is None:
            continue
        out.append({"wallet": w, "kind": kind, "mint": mint, "price_usd": px,
                    "usd": num(a, "volume_in_usd") or 0.0, "block_ts": ts,
                    "tx": a["tx_hash"], "event_id": f"{a['tx_hash']}:{w}:{kind}:{mint}"})
    out.sort(key=lambda x: (x["block_ts"], x["tx"]))
    # de-dup identical events inside one payload
    seen, uniq = set(), []
    for e in out:
        if e["event_id"] not in seen:
            seen.add(e["event_id"]); uniq.append(e)
    return uniq


def score_wallets(trade_lists, cfg):
    s = cfg["spy"]
    agg = {}
    for trades in trade_lists:
        per = {}
        for t in trades:
            per.setdefault(t["wallet"], []).append(t)
        for w, ts in per.items():
            if len(ts) > s["max_wallet_trades_in_window"] or ts[0]["kind"] != "buy":
                continue
            buy = sum(t["usd"] for t in ts if t["kind"] == "buy")
            sell = sum(t["usd"] for t in ts if t["kind"] == "sell")
            if sell <= 0 or buy <= 0:
                continue
            a = agg.setdefault(w, {"buy": 0.0, "sell": 0.0, "pools": 0})
            a["buy"] += buy; a["sell"] += sell; a["pools"] += 1
    res = []
    for w, a in agg.items():
        realized = a["sell"] - a["buy"]
        roi = realized / a["buy"]
        if realized >= s["min_wallet_realized_usd"] and roi >= s["min_wallet_roi"]:
            res.append({"wallet": w, "realized_usd": realized, "roi": roi, "pools": a["pools"]})
    res.sort(key=lambda r: r["realized_usd"], reverse=True)
    return res


def chase_gate(fill_px, leader_px, cfg):
    name = "max_chase"
    if not fill_px or not leader_px:
        return insufficient(name, "fill or leader price missing")
    chase = (fill_px / leader_px - 1) * 100
    if chase > cfg["spy"]["max_chase_pct"]:
        return block(name, f"fill {chase:+.1f}% vs leader entry > +{cfg['spy']['max_chase_pct']:.0f}%", round(chase, 2))
    return allow(name, round(chase, 2))


def wallet_cap_gate(open_cost_for_wallet, new_cost, equity, cfg):
    cap = cfg["spy"]["wallet_cap_pct"] * equity
    if open_cost_for_wallet + new_cost > cap + 1e-9:
        return block("wallet_cap", f"wallet allocation ${open_cost_for_wallet + new_cost:,.2f} > cap ${cap:,.2f}")
    return allow("wallet_cap", round(open_cost_for_wallet + new_cost, 2))


def should_demote(consec_losses, cfg):
    return consec_losses >= cfg["spy"]["demote_after_losses"]
