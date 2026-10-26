"""Fetch 5-minute OHLCV (first ~83h of each pool's life, 1000 candles) from GeckoTerminal,
or RugCheck reports (current state) per base mint. Usage: fetch_data.py ohlcv|rug"""
import json, sys, time
from http_cache import get
G = "https://api.geckoterminal.com/api/v2/networks/solana"
STABLE = {"So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
c = json.load(open("candidates.json"))
pools = [p for p in c["pools"] if p["base"] not in STABLE]
pools.sort(key=lambda p: p["created"])
MIN_AGE_S = 5400  # pools younger than 90 min at discovery cannot produce a 30m-age + 1h-lookback signal; skipped and counted
if len(sys.argv) > 1 and sys.argv[1] == "ohlcv":
    pools = [p for p in pools if time.time() - p["created"] >= MIN_AGE_S]
mode = sys.argv[1]
now = time.time()
if mode == "ohlcv":
    out = {}
    for i, p in enumerate(pools):
        before = int(min(now, p["created"] + 1000 * 300 + 300))
        u = f"{G}/pools/{p['address']}/ohlcv/minute?aggregate=5&limit=1000&currency=usd&token=base&before_timestamp={before}"
        st, body = get(u)
        n = len(((body or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list", []) or []) if st == 200 and isinstance(body, dict) else 0
        out[p["address"]] = {"status": st, "n": n, "url": u}
        print(i, p["name"], st, n, flush=True)
    json.dump(out, open("ohlcv_index.json", "w"), indent=1)
elif mode == "rug":
    out = {}
    mints = sorted({p["base"] for p in pools})
    for i, m in enumerate(mints):
        st, body = get(f"https://api.rugcheck.xyz/v1/tokens/{m}/report")
        out[m] = st
        print(i, m, st, flush=True)
    json.dump(out, open("rug_index.json", "w"), indent=1)
