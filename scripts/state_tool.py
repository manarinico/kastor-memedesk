"""State helpers for the GitHub Actions runner (paper desk; no secrets involved).

  python scripts/state_tool.py check DB           integrity_check + sanity (fails closed)
  python scripts/state_tool.py slim SRC DST [--keep-hours 6]
        consistent copy of SRC -> DST with every table intact except `snapshots`,
        which keeps only rows received within KEEP_HOURS of the newest snapshot
        (the engine only reads snapshots from the current cycle and RugCheck reports
        <= 30 min old; replay needs the full history, which is archived on the box).
        The newest snapshot row is always kept so INTEGER PRIMARY KEY ids never repeat.
  python scripts/state_tool.py info DB            one-line JSON summary (last cycle, equity)
"""
import json
import os
import sqlite3
import sys
import time

REQUIRED_TABLES = {"kv", "cycles", "snapshots", "decisions", "positions", "breaker_trips",
                   "equity_history", "wallets", "seen_events", "marks"}


def die(msg):
    print(f"::error::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def check(path):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        die(f"state DB {path} missing or empty - refusing to start from an empty DB")
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    r = c.execute("PRAGMA integrity_check").fetchall()
    if r != [("ok",)]:
        die(f"integrity_check failed on {path}: {r[:5]}")
    tables = {n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not REQUIRED_TABLES <= tables:
        die(f"state DB {path} lacks tables {sorted(REQUIRED_TABLES - tables)}")
    n_cycles = c.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
    n_pos = c.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    if n_cycles == 0:
        die(f"state DB {path} has 0 cycles - refusing to start from an empty DB")
    print(f"state OK: {path} integrity ok, {n_cycles} cycles, {n_pos} positions")


def info(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    cid, st, en, status, ver = c.execute(
        "SELECT id, started_at, ended_at, status, config_version FROM cycles ORDER BY id DESC LIMIT 1").fetchone()
    eq = {b: e for b, e in c.execute(
        "SELECT book, equity FROM equity_history WHERE id IN (SELECT MAX(id) FROM equity_history GROUP BY book)")}
    snaps = c.execute("SELECT COUNT(*), MIN(id), MAX(id) FROM snapshots").fetchone()
    return {"last_cycle": cid, "started_at": st, "ended_at": en, "status": status, "rules": ver,
            "equity": eq, "snapshots_kept": snaps[0], "snapshot_id_range": [snaps[1], snaps[2]]}


def slim(src, dst, keep_hours=6.0):
    if os.path.exists(dst):
        os.remove(dst)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    s.backup(d)  # consistent snapshot, includes WAL contents
    s.close()
    d.isolation_level = None
    d.execute("PRAGMA journal_mode=DELETE")
    trig = d.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='snapshots'").fetchall()
    newest_ts, max_id = d.execute("SELECT MAX(receipt_ts), MAX(id) FROM snapshots").fetchone()
    before = d.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    if newest_ts is not None:
        cutoff = newest_ts - keep_hours * 3600
        d.execute("BEGIN")
        for name, _ in trig:
            d.execute(f'DROP TRIGGER "{name}"')
        d.execute("DELETE FROM snapshots WHERE (receipt_ts IS NULL OR receipt_ts < ?) AND id <> ?", (cutoff, max_id))
        for _, sql in trig:
            d.execute(sql)
        d.execute("COMMIT")
    after = d.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    d.execute("VACUUM")
    d.close()
    check(dst)
    print(f"slim: snapshots {before} -> {after} (kept last {keep_hours}h of receipts; max id {max_id}), "
          f"{os.path.getsize(dst)/1e6:.1f} MB")


def main(argv):
    if len(argv) >= 2 and argv[0] == "check":
        check(argv[1])
    elif len(argv) >= 3 and argv[0] == "slim":
        kh = float(argv[argv.index("--keep-hours") + 1]) if "--keep-hours" in argv else 6.0
        slim(argv[1], argv[2], kh)
    elif len(argv) >= 2 and argv[0] == "info":
        print(json.dumps(info(argv[1])))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
