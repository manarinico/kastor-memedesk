"""SQLite persistence. Snapshots are append-only (UPDATE/DELETE raise)."""
import hashlib
import json
import sqlite3

from . import fills

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS cycles(
  id INTEGER PRIMARY KEY, started_at REAL, ended_at REAL, mode TEXT,
  config_version TEXT, status TEXT, timings TEXT, summary TEXT, source_cycle_id INTEGER);
CREATE TABLE IF NOT EXISTS snapshots(
  id INTEGER PRIMARY KEY, cycle_id INTEGER, seq INTEGER, source TEXT, kind TEXT, key TEXT,
  url TEXT, units TEXT, ok INTEGER, error TEXT, fetch_ts REAL, receipt_ts REAL,
  latency_ms REAL, payload_sha256 TEXT, payload TEXT);
CREATE INDEX IF NOT EXISTS snap_kind_key ON snapshots(kind, key, receipt_ts);
CREATE INDEX IF NOT EXISTS snap_cycle ON snapshots(cycle_id);
CREATE TRIGGER IF NOT EXISTS snapshots_no_update BEFORE UPDATE ON snapshots
  BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_delete BEFORE DELETE ON snapshots
  BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY, cycle_id INTEGER, ts REAL, book TEXT, bot TEXT, mint TEXT,
  symbol TEXT, stage TEXT, state TEXT, reason_code TEXT, reason TEXT, snapshot_id INTEGER,
  aux_snapshot_ids TEXT, config_version TEXT, details TEXT);
CREATE INDEX IF NOT EXISTS dec_cycle ON decisions(cycle_id);
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY, proposal_key TEXT UNIQUE NOT NULL, book TEXT, bot TEXT, mint TEXT,
  symbol TEXT, pair_address TEXT, dex_id TEXT, leader_wallet TEXT, leader_px REAL,
  leader_ts REAL, qty REAL, entry_mid REAL, entry_px REAL, entry_cost_usd REAL,
  entry_fee_usd REAL, entry_slip_usd REAL, entry_ts REAL, entry_snapshot_id INTEGER,
  entry_decision_id INTEGER, entry_liquidity REAL, fill_lag_s REAL, config_version TEXT,
  status TEXT, exit_ts REAL, exit_mid REAL, exit_px REAL, exit_proceeds_usd REAL,
  exit_fee_usd REAL, exit_slip_usd REAL, exit_reason TEXT, exit_snapshot_id INTEGER,
  exit_config_version TEXT, pnl_usd REAL);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_per_token
  ON positions(book, bot, mint) WHERE status='open';
CREATE TABLE IF NOT EXISTS breaker_trips(
  id INTEGER PRIMARY KEY, cycle_id INTEGER, ts REAL, breaker TEXT, scope TEXT,
  detail TEXT, config_version TEXT);
CREATE TABLE IF NOT EXISTS equity_history(
  id INTEGER PRIMARY KEY, cycle_id INTEGER, ts REAL, book TEXT, equity REAL, cash REAL,
  open_value REAL, n_open INTEGER, n_unpriced INTEGER);
CREATE TABLE IF NOT EXISTS wallets(
  address TEXT PRIMARY KEY, status TEXT, source TEXT, discovered_at REAL, score_usd REAL,
  consec_losses INTEGER DEFAULT 0, copies INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
  losses INTEGER DEFAULT 0, pnl_usd REAL DEFAULT 0, demoted_at REAL, note TEXT);
CREATE TABLE IF NOT EXISTS seen_events(
  event_id TEXT PRIMARY KEY, cycle_id INTEGER, ts REAL, wallet TEXT, kind TEXT, mint TEXT,
  price_usd REAL, block_ts REAL, outcome TEXT);
CREATE TABLE IF NOT EXISTS marks(
  mint TEXT, pair_address TEXT, price_usd REAL, liquidity_usd REAL, observed_ts REAL,
  snapshot_id INTEGER, PRIMARY KEY(mint, pair_address));
CREATE TABLE IF NOT EXISTS side_positions(
  position_id INTEGER PRIMARY KEY, book TEXT, stop_trigger_px REAL, take_profit_px REAL,
  time_stop_ts REAL, stop_rule TEXT, source TEXT, ledger_sha256 TEXT, seeded_cycle_id INTEGER,
  seeded_at REAL, config_version TEXT);
CREATE TABLE IF NOT EXISTS side_book_log(
  id INTEGER PRIMARY KEY, cycle_id INTEGER, ts REAL, book TEXT, position_id INTEGER, mint TEXT,
  event TEXT, observed_px REAL, observed_ts REAL, liquidity_usd REAL, snapshot_id INTEGER,
  value_usd REAL, gross_value_usd REAL, unrealized_usd REAL, stop_trigger_px REAL, to_stop_pct REAL,
  trigger_px REAL, modeled_fill_px REAL, gap_s REAL, missed_slots INTEGER, fill_flag TEXT,
  details TEXT, config_version TEXT);
"""

BOOKS = ("desk", "baseline")
# Isolated side books (r8): own start balance, never in desk equity/exposure/breakers/scorecard.
SIDE_BOOKS = ("nico",)


def connect(path):
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)  # autocommit; explicit txns
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


class txn:
    """BEGIN IMMEDIATE ... COMMIT / ROLLBACK."""
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, et, ev, tb):
        self.conn.execute("ROLLBACK" if et else "COMMIT")
        return False


def kv_get(conn, k, default=None):
    r = conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(r[0]) if r else default


def kv_set(conn, k, v):
    conn.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                 (k, json.dumps(v)))


def kv_setdefault(conn, k, v):
    conn.execute("INSERT OR IGNORE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v)))
    return kv_get(conn, k)


def payload_sha(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def insert_snapshot(conn, cycle_id, seq, r):
    cur = conn.execute(
        "INSERT INTO snapshots(cycle_id,seq,source,kind,key,url,units,ok,error,fetch_ts,"
        "receipt_ts,latency_ms,payload_sha256,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cycle_id, seq, r.source, r.kind, r.key, r.url, r.units, int(bool(r.ok)), r.error,
         r.fetch_ts, r.receipt_ts, r.latency_ms, payload_sha(r.payload) if r.ok else None,
         json.dumps(r.payload) if r.ok else None))
    return cur.lastrowid


def open_positions(conn, book=None, bot=None):
    q, a = "SELECT * FROM positions WHERE status='open'", []
    if book:
        q += " AND book=?"; a.append(book)
    if bot:
        q += " AND bot=?"; a.append(bot)
    return [dict(r) for r in conn.execute(q + " ORDER BY id", a)]


def latest_mark(conn, mint, pair):
    r = conn.execute("SELECT * FROM marks WHERE mint=? AND pair_address=?", (mint, pair)).fetchone()
    return dict(r) if r else None


def update_mark(conn, mint, pair, price, liq, observed_ts, snapshot_id):
    """Out-of-order safe: an observation older than (or equal to) the stored one is ignored.
    Returns True if applied."""
    cur = conn.execute(
        "INSERT INTO marks(mint,pair_address,price_usd,liquidity_usd,observed_ts,snapshot_id) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(mint,pair_address) DO UPDATE SET "
        "price_usd=excluded.price_usd, liquidity_usd=excluded.liquidity_usd, "
        "observed_ts=excluded.observed_ts, snapshot_id=excluded.snapshot_id "
        "WHERE excluded.observed_ts > marks.observed_ts",
        (mint, pair, price, liq, observed_ts, snapshot_id))
    return cur.rowcount > 0


def position_value(conn, p, costs=None):
    """What an open position is worth if we sold it now: the paper SELL fill (fees, slippage,
    price impact) at the latest mark's price and pool liquidity. A drained pool therefore shows
    as a near-total loss instead of being valued at the quoted price. Returns (value, priced)."""
    m = latest_mark(conn, p["mint"], p["pair_address"])
    if not (m and m["price_usd"]):
        return p["qty"] * p["entry_mid"], False  # no price yet: carried at entry, flagged unpriced
    if costs is None:
        return p["qty"] * m["price_usd"], True
    if not m["liquidity_usd"]:
        return p["qty"] * m["price_usd"], False  # price but no liquidity: can't model the exit, flagged
    f = fills.guard_sell(fills.sell(m["price_usd"], p["qty"], m["liquidity_usd"], costs), p["entry_mid"])
    return (f["proceeds_usd"] if f else 0.0), True


def book_state(conn, book, start_usd, costs=None):
    """Cash and equity are DERIVED from the positions table every time, so a restart
    can never double-count P&L. Open positions are valued at liquidation value when
    `costs` is given (the engine and the report always pass it)."""
    r = conn.execute(
        "SELECT COALESCE(SUM(entry_cost_usd),0), "
        "COALESCE(SUM(CASE WHEN status='closed' THEN exit_proceeds_usd ELSE 0 END),0) "
        "FROM positions WHERE book=?", (book,)).fetchone()
    cash = start_usd - r[0] + r[1]
    open_value, unpriced = 0.0, 0
    opens = open_positions(conn, book=book)
    for p in opens:
        v, priced = position_value(conn, p, costs)
        open_value += v
        unpriced += 0 if priced else 1
    return {"cash": cash, "open_value": open_value, "equity": cash + open_value,
            "n_open": len(opens), "n_unpriced": unpriced}
