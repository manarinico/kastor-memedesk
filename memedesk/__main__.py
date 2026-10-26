"""CLI:  python -m memedesk {run_cycle,report,replay,probe}"""
import argparse
import json
import os
import sys
import time

from . import db, engine, report, replay
from .config import load_config
from .feed import LiveFeed

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.environ.get("MEMEDESK_DB", os.path.join(HERE, "data", "memedesk.sqlite"))


DEFAULT_LOG = os.environ.get("MEMEDESK_LOG", os.path.join(HERE, "data", "cron.log"))
# Where the cycle ran ("box" by default; the GitHub Actions workflow sets "github-actions").
# Plumbing only: logged and stored in kv as cycle_host:<id>; not part of the rules hash.
HOST = os.environ.get("MEMEDESK_HOST", "box")


def _stamp():
    return time.strftime("%Y-%m-%d %H:%M:%S %Z")


class _Out:
    """Writes each line to stdout and appends it to the cron log (so a cycle triggered from
    anywhere - the loop, cron, a scheduled routine, by hand - always leaves a line there)."""
    def __init__(self, log_path):
        self.log_path = log_path

    def __call__(self, line=""):
        print(line, flush=True)
        if self.log_path:
            try:
                with open(self.log_path, "a") as f:
                    f.write(line + "\n")
            except OSError:
                pass


def _run_cycle_cmd(a):
    """Safe to trigger at any moment: a non-blocking lock (data/.cycle.lock, separate from the
    optional outer `flock data/.lock`) makes overlapping triggers skip with a logged line;
    every exit path - success, error, SIGTERM from `timeout`, lock held - logs one
    timestamped line. Re-running is idempotent (proposal keys are unique; snapshots append)."""
    import fcntl
    import signal
    out = _Out(a.log)
    lock_path = os.path.join(os.path.dirname(os.path.abspath(a.db)), ".cycle.lock")
    lf = open(lock_path, "a")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        out(f"[{_stamp()}] run_cycle SKIPPED: another cycle holds {lock_path} (pid {os.getpid()})")
        return 0

    class _Killed(BaseException):
        pass

    def _on_term(signum, _frame):
        raise _Killed(signal.Signals(signum).name)

    old = {sig: signal.signal(sig, _on_term) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        return _run_locked(a, out, _Killed)
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)
        lf.close()


def _run_locked(a, out, _Killed):
    t0, wall0 = time.monotonic(), time.time()
    out(f"[{_stamp()}] run_cycle start (pid {os.getpid()}, host {HOST})")
    conn = None
    try:
        cfg = load_config()
        conn = db.connect(a.db)
        res = engine.run_cycle(conn, cfg, LiveFeed(cfg))
    except BaseException as e:  # noqa: BLE001 - log everything, including SIGTERM/Ctrl-C
        dur = time.monotonic() - t0
        what = f"killed by {e}" if isinstance(e, _Killed) else f"{type(e).__name__}: {str(e)[:300]}"
        cid = _mark_failed(conn, what, wall0)
        if cid:
            try:
                db.kv_set(conn, f"cycle_host:{cid}", HOST)
            except Exception:
                pass
        out(f"[{_stamp()}] run_cycle FAILED after {dur:.1f}s"
            + (f" (cycle {cid})" if cid else "") + f": {what}")
        if not isinstance(e, (_Killed, KeyboardInterrupt)):
            import traceback
            traceback.print_exc()
        return 1
    try:
        db.kv_set(conn, f"cycle_host:{res['cycle_id']}", HOST)
    except Exception:
        pass
    s, t = res["summary"], res["timings"]
    out(f"[{_stamp()}] cycle {res['cycle_id']} done in {t['total_s']}s (fetch {t['fetch_wall_s']}s, decide {t['decision_s']}s) "
        f"rules {cfg['version_tag']} host {HOST}")
    out(f"  candidates {s['candidates']}, priced {s['priced']}, Scout-approved {s['approved']}, "
        f"entries {s['entries']}, exits {s['exits']}, breaker trips {s['trips']}, active spy wallets {s['active_wallets']}")
    out(f"  desk book ${s['desk_equity']:,.2f} | baseline book ${s['baseline_equity']:,.2f}")
    for b in s["desk_breakers"]:
        out(f"  breaker {b['name']}: {b['state']} {b['reason'] if b['state'] != 'ALLOW' else ''}")
    if s["sources_failed"]:
        out("  failed sources this cycle: " + "; ".join(s["sources_failed"]))
    if s.get("fetch_budget_hit"):
        out(f"  FETCH BUDGET EXHAUSTED ({s['fetch_budget_hit']}): remaining data treated as missing; no entries on it")
    out(f"  fetch ms by source: {t['fetch_ms_by_kind']}")
    out(f"  data age at entry check: {t['data_age_at_entry_check_s']}; simulated fill lag: {t['simulated_fill_lag_s']}")
    return 0


def _mark_failed(conn, what, since):
    """Close out this process's half-finished cycle row (we hold the cycle lock, so the only
    'running' live row newer than any finished one is ours)."""
    if conn is None:
        return None
    try:
        r = conn.execute("SELECT id FROM cycles WHERE mode='live' AND status='running' AND started_at >= ? "
                         "ORDER BY id DESC LIMIT 1", (since - 1,)).fetchone()
        if r is None:
            return None
        conn.execute("UPDATE cycles SET status='failed', ended_at=?, summary=? WHERE id=?",
                     (time.time(), json.dumps({"error": what}), r[0]))
        return r[0]
    except Exception:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="memedesk", description="Paper-trading memecoin desk (no real money).")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--log", default=DEFAULT_LOG, help="cron log run_cycle appends to ('' = stdout only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run_cycle", help="one scan/decide/update pass")
    sub.add_parser("report", help="plain-English scorecard")
    rp = sub.add_parser("replay", help="re-run strategy over saved snapshots (dev vs eval)")
    rp.add_argument("--dev-frac", type=float, default=0.7)
    rp.add_argument("--fee-bps", type=float)
    rp.add_argument("--slip-bps", type=float)
    rp.add_argument("--latency-s", type=float)
    rp.add_argument("--network-fee-usd", type=float)
    rp.add_argument("--out-dir", default=os.path.join(HERE, "data"))
    sub.add_parser("probe", help="check which free data sources respond right now")
    a = ap.parse_args(argv)
    os.makedirs(os.path.dirname(a.db), exist_ok=True)

    if a.cmd == "run_cycle":
        return _run_cycle_cmd(a)

    if a.cmd == "report":
        cfg = load_config()
        conn = db.connect(a.db)
        print(report.render(report.compute(conn, cfg), cfg))
        return 0

    if a.cmd == "replay":
        ov = {"costs": {}}
        if a.fee_bps is not None: ov["costs"]["dex_fee_bps"] = a.fee_bps
        if a.slip_bps is not None: ov["costs"]["base_slippage_bps"] = a.slip_bps
        if a.latency_s is not None: ov["costs"]["latency_s"] = a.latency_s
        if a.network_fee_usd is not None: ov["costs"]["network_fee_usd"] = a.network_fee_usd
        cfg = load_config(ov)
        src = db.connect(a.db)
        cycles = replay.source_cycles(src)
        if len(cycles) < 2:
            print(f"only {len(cycles)} recorded live cycle(s); need at least 2 to split dev/eval")
            return 1
        dev, ev = replay.split(cycles, a.dev_frac)
        for name, cs in (("DEVELOPMENT", dev), ("EVALUATION", ev)):
            if not cs:
                print(f"{name}: no cycles"); continue
            out = replay.replay(a.db, cfg, cs, os.path.join(a.out_dir, f"replay_{name.lower()}.sqlite"))
            print(report.render(report.compute(out, cfg), cfg,
                                f"REPLAY {name}: {len(cs)} cycles, costs {cfg['costs']}"))
            print()
        return 0

    if a.cmd == "probe":
        cfg = load_config()
        f = LiveFeed(cfg)
        rs = f.discover()
        mint = None
        for r in rs:
            if r.kind == "dex_profiles" and r.ok:
                mint = next((t["tokenAddress"] for t in r.payload if t.get("chainId") == "solana"), None)
        if mint:
            rs += f.dex_tokens([mint]) + [f.rug_report(mint)]
        for r in rs:
            print(f"{'OK  ' if r.ok else 'FAIL'} {r.source:13s} {r.kind:16s} {r.latency_ms:7.0f}ms {r.error or ''}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
