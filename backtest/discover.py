"""Discover candidate Solana pools created in the last 14 days from free GeckoTerminal lists."""
import json, sys, time
from datetime import datetime, timezone
from http_cache import get
G = "https://api.geckoterminal.com/api/v2/networks/solana"
lists = []
for sort in ("h24_volume_usd_desc", "h24_tx_count_desc"):
    lists += [f"{G}/pools?page={p}&sort={sort}" for p in range(1, 11)]
lists += [f"{G}/trending_pools?page={p}&duration={d}" for d in ("24h", "6h") for p in range(1, 11)]
lists += [f"{G}/new_pools?page={p}" for p in range(1, 11)]
for dex in ("pumpswap", "raydium", "meteora", "meteora-damm-v2", "raydium-clmm", "raydium-launchlab"):
    lists += [f"{G}/dexes/{dex}/pools?page={p}&sort=h24_volume_usd_desc" for p in range(1, 6)]
# round 2 (added after first pass, to widen coverage of older pools)
for dex in ("pumpswap", "raydium-launchlab", "meteora", "meteora-damm-v2"):
    lists += [f"{G}/dexes/{dex}/pools?page={p}&sort=h24_volume_usd_desc" for p in range(6, 11)]
    lists += [f"{G}/dexes/{dex}/pools?page={p}&sort=h24_tx_count_desc" for p in range(1, 11)]
now = time.time(); cutoff = now - 14 * 86400
pools = {}
stats = []
for u in lists:
    st, body = get(u)
    n = 0
    if st == 200 and body:
        for p in body.get("data", []):
            a = p["attributes"]; r = p["relationships"]
            try: created = datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00")).timestamp()
            except Exception: continue
            base = r["base_token"]["data"]["id"].replace("solana_", "")
            quote = r["quote_token"]["data"]["id"].replace("solana_", "")
            if created < cutoff: continue
            n += 1
            pools.setdefault(a["address"], {"address": a["address"], "name": a["name"], "base": base, "quote": quote,
                "dex": r["dex"]["data"]["id"], "created": created, "reserve_now": float(a.get("reserve_in_usd") or 0), "price_now": float(a.get("base_token_price_usd") or 0), "listed_at": time.time(),
                "source": u.split("/solana/")[1]})
    stats.append((u.split("/solana/")[1], st, n))
    print(stats[-1], len(pools), flush=True)
json.dump({"generated": now, "lists": stats, "pools": list(pools.values())}, open("candidates.json", "w"), indent=1)
print("TOTAL", len(pools))
