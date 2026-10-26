"""Historical backtest of the memedesk Gambler (+ Scout gates + Risk Desk) on GeckoTerminal 5m candles.
Reuses memedesk.config (rule constants), memedesk.fills (fee/slippage model),
memedesk.scout.safety_gates (RugCheck gate logic) and memedesk.risk breakers. Read-only imports."""
import bisect, csv, json, math, os, sys, time
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memedesk.config import load_config
from memedesk import fills, risk
from memedesk.scout import safety_gates
from memedesk.gates import combine, ALLOW
from http_cache import get

NY = ZoneInfo("America/New_York")
CAD = 20 * 60
C5 = 300
HERE = os.path.dirname(os.path.abspath(__file__))


class Pool:
    def __init__(self, meta, ohlcv):
        self.m = meta
        rows = sorted(ohlcv, key=lambda r: r[0])
        self.ts = [int(r[0]) for r in rows]
        self.o = [float(r[1]) for r in rows]; self.h = [float(r[2]) for r in rows]
        self.l = [float(r[3]) for r in rows]; self.c = [float(r[4]) for r in rows]
        self.v = [float(r[5]) for r in rows]
        self.data_end = meta["created"] + 1000 * C5  # fetched window end
        self._fc = {}

    def idx_closed(self, t):
        """index of last candle fully closed at time t (ts + 300 <= t), or -1"""
        return bisect.bisect_right(self.ts, t - C5) - 1

    def mid(self, t):
        i = self.idx_closed(t)
        return self.c[i] if i >= 0 else None

    def liq(self, t):
        """Liquidity estimate: current reserve scaled by sqrt(price ratio) (constant-product approx)."""
        p = self.mid(t); pn = self.m.get("price_now") or 0; rn = self.m.get("reserve_now") or 0
        if not p or pn <= 0 or rn <= 0:
            return None
        return rn * math.sqrt(p / pn)

    def features(self, t):
        if t in self._fc:
            return self._fc[t]
        r = self._features(t); self._fc[t] = r
        return r

    def _features(self, t):
        i = self.idx_closed(t)
        if i < 0:
            return None
        p = self.c[i]
        j = self.idx_closed(t - 3600)
        p1h = self.c[j] if j >= 0 else self.o[0]
        k = self.idx_closed(t - C5)
        p5 = self.c[k] if k >= 0 else self.o[0]
        lo = bisect.bisect_left(self.ts, t - 3600)
        up = dn = vol = 0.0
        for q in range(lo, i + 1):
            vol += self.v[q]
            if self.c[q] > self.o[q]: up += self.v[q]
            elif self.c[q] < self.o[q]: dn += self.v[q]
        ratio = up / dn if dn > 0 else (float("inf") if up > 0 else 0.0)
        return {"h1": (p / p1h - 1) * 100 if p1h > 0 else None, "m5": (p / p5 - 1) * 100 if p5 > 0 else None,
                "bs": ratio, "vol": vol, "mid": p}


def momentum_ok(f, g):
    return (f and f["h1"] is not None and g["min_h1_change_pct"] <= f["h1"] <= g["max_h1_change_pct"]
            and f["m5"] >= g["min_m5_change_pct"] and f["bs"] >= g["min_buy_sell_ratio_h1"]
            and f["vol"] >= g["min_h1_volume_usd"])


def run(pools, safety_ok, cfg, book, t0, t1, gated, label):
    g, costs, rk = cfg["gambler"], cfg["costs"], cfg["risk"]
    cash = book; open_pos = []; trades = []; curve = []
    last_entry = {}; closed_pnls = []; last_exit_ts = None; day_key = None; day_start = book
    peak = book; mdd = 0.0
    brk = {"daily_loss_cycles": 0, "consec_loss_cycles": 0}
    t = t0
    while t <= t1:
        # 1) exits on candles closed since last check (conservative: SL on lows, fill at worse of SL and mark)
        still = []
        for pos in open_pos:
            P = pos["pool"]
            lo_i = bisect.bisect_left(P.ts, pos["last_check"]); hi_i = P.idx_closed(t)
            mark = P.mid(t)
            sl_px = pos["entry_mid"] * (1 - g["stop_loss_pct"] / 100)
            tp_px = pos["entry_mid"] * (1 + g["take_profit_pct"] / 100)
            reason = px = None
            lows = [P.l[q] for q in range(lo_i, hi_i + 1)] if hi_i >= lo_i else []
            liq_now = P.liq(t)
            if gated and liq_now is not None and pos["entry_liq"]:
                if risk.liquidity_drop_breaker(pos["entry_liq"], liq_now, cfg).state != ALLOW:
                    reason, px = "liquidity_drop", mark
            if reason is None and lows and min(lows) <= sl_px:
                reason, px = "stop_loss", min(sl_px, mark)
            elif reason is None and mark is not None and mark >= tp_px:
                reason, px = "take_profit", mark
            elif reason is None and (t - pos["entry_ts"]) >= g["time_stop_min"] * 60:
                reason, px = "time_stop", mark
            pos["last_check"] = t
            if reason is None:
                still.append(pos); continue
            fl = fills.sell(px, pos["qty"], liq_now or pos["entry_liq"], costs)
            pnl = fl["proceeds_usd"] - pos["cost"]
            cash += fl["proceeds_usd"]
            closed_pnls.insert(0, pnl); last_exit_ts = t
            trades.append({"run": label, "pool": P.m["address"], "name": P.m["name"], "dex": P.m["dex"],
                "entry_time_et": datetime.fromtimestamp(pos["entry_ts"], NY).strftime("%Y-%m-%d %H:%M"),
                "exit_time_et": datetime.fromtimestamp(t, NY).strftime("%Y-%m-%d %H:%M"),
                "pool_age_h_at_entry": round((pos["entry_ts"] - P.m["created"]) / 3600, 2),
                "entry_mid": pos["entry_mid"], "entry_fill_px": pos["px"], "exit_mid": px, "exit_fill_px": fl["px"],
                "reason": reason, "cost_usd": round(pos["cost"], 4), "proceeds_usd": round(fl["proceeds_usd"], 4),
                "pnl_usd": round(pnl, 4), "pnl_pct": round(pnl / pos["cost"] * 100, 2),
                "dex_fees_usd": round(pos["dexfee"] + fl["fee_usd"] - costs["network_fee_usd"], 4),
                "network_fees_usd": round(2 * costs["network_fee_usd"], 4),
                "slippage_usd": round(pos["slip"] + fl["slippage_usd"], 4),
                "entry_liq_est": round(pos["entry_liq"] or 0), "h1": round(pos["f"]["h1"], 1), "m5": round(pos["f"]["m5"], 2),
                "bs_proxy": round(min(pos["f"]["bs"], 999), 2), "vol_h1": round(pos["f"]["vol"])})
        open_pos = still
        # 2) equity at liquidation value
        eq = cash
        for pos in open_pos:
            m = pos["pool"].mid(t) or pos["entry_mid"]
            fl = fills.sell(m, pos["qty"], pos["pool"].liq(t) or pos["entry_liq"], costs)
            eq += fl["proceeds_usd"] if fl else 0
        dk = datetime.fromtimestamp(t, NY).date()
        if dk != day_key:
            day_key, day_start = dk, eq
        peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak * 100)
        curve.append((t, eq))
        # 3) breakers
        can_enter = True
        if gated:
            if risk.daily_loss_breaker(eq, day_start, cfg).state != ALLOW:
                can_enter = False; brk["daily_loss_cycles"] += 1
            if risk.consecutive_loss_breaker(closed_pnls, last_exit_ts, t, cfg).state != ALLOW:
                can_enter = False; brk["consec_loss_cycles"] += 1
        # 4) entries
        if can_enter and t < t1 - g["time_stop_min"] * 60:
            held = {p["pool"].m["base"] for p in open_pos}
            cands = []
            for P in pools:
                if t < P.ts[0] or t > P.ts[-1] + 3600:
                    continue
                if t < P.m["created"] or t > P.data_end - g["time_stop_min"] * 60 - CAD:
                    continue
                b = P.m["base"]
                if b in held or t - last_entry.get(b, -1e18) < g["reentry_cooldown_min"] * 60:
                    continue
                age_min = (t - P.m["created"]) / 60
                liq = P.liq(t)
                if liq is None: continue
                if gated:
                    s = cfg["scout"]
                    if not (s["min_age_min"] <= age_min <= s["max_age_hours"] * 60): continue
                    if liq < s["min_liquidity_usd"]: continue
                    if not safety_ok.get(b): continue
                else:
                    if liq < cfg["baseline"]["min_liquidity_usd"]: continue
                f = P.features(t)
                if not momentum_ok(f, g): continue
                cands.append((f["vol"], P, f, liq))
            cands.sort(key=lambda x: -x[0])
            for _, P, f, liq in cands:
                if len(open_pos) >= g["max_open"]: break
                if P.m["base"] in held: continue
                budget = g["risk_pct"] * eq
                if budget > cash: break
                fl = fills.buy(f["mid"], budget, liq, costs)
                if not fl: continue
                cash -= budget
                open_pos.append({"pool": P, "entry_ts": t, "last_check": t, "entry_mid": f["mid"], "px": fl["px"],
                    "qty": fl["qty"], "cost": budget, "dexfee": fl["fee_usd"] - costs["network_fee_usd"],
                    "slip": fl["slippage_usd"], "entry_liq": liq, "f": f})
                held.add(P.m["base"]); last_entry[P.m["base"]] = t
        t += CAD
    # force-close leftovers at last mark (should be none; entries stop 4h before end)
    for pos in open_pos:
        P = pos["pool"]; m = P.mid(t1) or pos["entry_mid"]
        fl = fills.sell(m, pos["qty"], P.liq(t1) or pos["entry_liq"], costs); cash += fl["proceeds_usd"]
    return {"label": label, "start": book, "end": curve[-1][1] if curve else book, "trades": trades, "mdd": mdd, "curve": curve, "breakers": brk}


def summarize(r):
    T = r["trades"]; w = [x["pnl_usd"] for x in T if x["pnl_usd"] > 0]; l = [x["pnl_usd"] for x in T if x["pnl_usd"] <= 0]
    gross = [x for x in T]
    return {"run": r["label"], "start": r["start"], "end": round(r["end"], 2), "pnl": round(r["end"] - r["start"], 2),
        "pnl_pct": round((r["end"] / r["start"] - 1) * 100, 2), "n": len(T),
        "win_rate": round(len(w) / len(T) * 100, 1) if T else None,
        "avg_win": round(sum(w) / len(w), 3) if w else None, "avg_loss": round(sum(l) / len(l), 3) if l else None,
        "mdd_pct": round(r["mdd"], 2),
        "dex_fees": round(sum(x["dex_fees_usd"] for x in T), 2), "net_fees": round(sum(x["network_fees_usd"] for x in T), 2),
        "slippage": round(sum(x["slippage_usd"] for x in T), 2),
        "best": max(T, key=lambda x: x["pnl_usd"]) if T else None, "worst": min(T, key=lambda x: x["pnl_usd"]) if T else None,
        "gross_price_pnl": round(sum(x["cost_usd"] * (x["exit_mid"] / x["entry_mid"] - 1) for x in T), 2),
        "breakers": r["breakers"], "reasons": {k: sum(1 for x in T if x["reason"] == k) for k in ("take_profit", "stop_loss", "time_stop", "liquidity_drop")}}


def load():
    c = json.load(open(os.path.join(HERE, "candidates.json")))
    idx = json.load(open(os.path.join(HERE, "ohlcv_index.json")))
    pools, nodata = [], []
    for p in c["pools"]:
        e = idx.get(p["address"])
        if not e: continue
        st, body = get(e["url"])
        rows = (((body or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") if st == 200 and isinstance(body, dict) else None
        if not rows: nodata.append((p["address"], p["name"], st)); continue
        pools.append(Pool(p, rows))
    return c, pools, nodata


def safety_map(pools, cfg):
    out, detail = {}, {}
    now = time.time()
    for P in pools:
        b = P.m["base"]
        if b in out: continue
        st, rep = get(f"https://api.rugcheck.xyz/v1/tokens/{b}/report")
        if st != 200 or not isinstance(rep, dict):
            out[b] = False; detail[b] = f"no RugCheck report (HTTP {st})"; continue
        gs = safety_gates(rep, now, {"pairAddress": P.m["address"]}, now, cfg)
        v = combine(gs)
        out[b] = v.state == ALLOW; detail[b] = v.state + ":" + v.name + ":" + v.reason
    return out, detail
