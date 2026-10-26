import csv, json, os, sys, time
from datetime import datetime
from backtest import load, safety_map, run, summarize, NY, CAD
from memedesk.config import load_config
cfg = load_config()
c, pools, nodata = load()
MODE = os.environ.get("BT_MODE", "clean")
spiky = []
for P in pools:
    if any(P.c[i - 1] > 0 and P.c[i] / P.c[i - 1] > 10 for i in range(1, len(P.c))):
        spiky.append(P.m["address"])
if MODE == "clean":
    pools = [P for P in pools if P.m["address"] not in set(spiky)]
SUF = "" if MODE == "clean" else "_raw"
safe, detail = safety_map(pools, cfg)
now = time.time()
t1 = int(now // CAD * CAD)
t0 = t1 - 14 * 86400
# only start where data exists
first = min(P.ts[0] for P in pools)
t0 = max(t0, int(first // CAD * CAD))
split = t0 + int((t1 - t0) * 0.7 // CAD * CAD)
results = {}
all_trades = []
for book in (150.0, 1000.0):
    for gated, nm in ((True, "desk"), (False, "baseline")):
        for per, a, b in (("full", t0, t1), ("dev", t0, split), ("heldout", split, t1)):
            lab = f"{nm}_{int(book)}_{per}"
            r = run(pools, safe, cfg, book, a, b, gated, lab)
            results[lab] = summarize(r)
            all_trades += r["trades"]
            print(lab, {k: v for k, v in results[lab].items() if k not in ("best", "worst")}, flush=True)
with open(f"trades{SUF}.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(all_trades[0].keys())); w.writeheader(); w.writerows(all_trades)
meta = {"t0_et": datetime.fromtimestamp(t0, NY).isoformat(), "split_et": datetime.fromtimestamp(split, NY).isoformat(),
        "t1_et": datetime.fromtimestamp(t1, NY).isoformat(), "n_candidates": len(c["pools"]), "n_with_data": len(pools),
        "nodata": nodata, "mode": MODE, "n_spiky_excluded": len(spiky) if MODE == "clean" else 0, "n_spiky": len(spiky), "n_too_young": sum(1 for p in c["pools"] if now - p["created"] < 5400),
        "n_dead_now": sum(1 for P in pools if (P.m.get("reserve_now") or 0) < 1000),
        "n_not_fetched": len(c["pools"]) - len(pools) - len(nodata), "n_in_window": sum(1 for P in pools if P.ts[-1] >= t0), "n_rug_missing": sum(1 for v in detail.values() if v.startswith("BLOCK:no") or "no RugCheck" in v), "n_safe": sum(1 for v in safe.values() if v), "n_tokens": len(safe),
        "safety_reasons": {}, "lists": c["lists"]}
for v in detail.values():
    k = v.split(":")[0] + ":" + v.split(":")[1]; meta["safety_reasons"][k] = meta["safety_reasons"].get(k, 0) + 1
json.dump({"meta": meta, "results": results}, open(f"results{SUF}.json", "w"), indent=1, default=str)
print(json.dumps(meta["safety_reasons"]), meta["n_safe"], meta["n_tokens"], len(pools), len(nodata))
