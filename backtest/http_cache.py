"""Cached, rate-limited GET for free public APIs. Every response is cached under cache/."""
import hashlib, json, os, time, urllib.request, urllib.error
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
os.makedirs(CACHE, exist_ok=True)
_last = {}
SPACING = {"api.geckoterminal.com": 2.3, "api.rugcheck.xyz": 0.6, "api.dexscreener.com": 0.3}
LOG = os.path.join(CACHE, "_fetch_log.jsonl")

def get(url, refresh=False):
    key = hashlib.sha1(url.encode()).hexdigest()
    path = os.path.join(CACHE, key + ".json")
    if os.path.exists(path) and not refresh:
        with open(path) as f:
            c = json.load(f)
        return c["status"], c["body"]
    host = url.split("/")[2]
    for attempt in range(4):
        wait = SPACING.get(host, 1.0) - (time.time() - _last.get(host, 0))
        if wait > 0: time.sleep(wait)
        _last[host] = time.time()
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "memedesk-backtest/0.1"})
            with urllib.request.urlopen(req, timeout=20) as r:
                status, body = r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            status, body = e.code, None
        except Exception as e:
            status, body = -1, str(e)
        if status == 429 or status == -1:
            time.sleep(15 * (attempt + 1)); continue
        break
    with open(LOG, "a") as f:
        f.write(json.dumps({"url": url, "status": status, "ts": time.time()}) + "\n")
    if status in (200, 404):
        with open(path, "w") as f:
            json.dump({"url": url, "status": status, "fetched": time.time(), "body": body}, f)
    return status, body
