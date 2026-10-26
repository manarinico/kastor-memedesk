"""Replay Scout gates v1.1 (Token-2022 transfer_fee + transfer_restriction) over stored data.

1. Every stored ok RugCheck report (archive + live DB, deduped by snapshot id): new gate result.
2. Every stored Scout decision that read a RugCheck report: old verdict vs old gates + v1.1 gates.
Read-only. Usage: .venv/bin/python scripts/replay_scout_v11.py
"""
import collections
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memedesk import scout  # noqa: E402
from memedesk.config import load_config  # noqa: E402
from memedesk.gates import Gate, combine  # noqa: E402

DBS = ["data/archive/snapshots_archive.sqlite", "data/memedesk.sqlite"]
cfg = load_config()
reports = {}
for f in DBS:
    c = sqlite3.connect(f)
    for sid, key, p in c.execute("SELECT id, key, payload FROM snapshots WHERE kind='rug_report' AND ok=1"):
        reports[sid] = (key, json.loads(p))

states = collections.Counter()
hits = []
for sid, (mint, rep) in sorted(reports.items()):
    for g in scout.token2022_gates(rep, cfg):
        states[(g.name, g.state)] += 1
        if g.state != "ALLOW":
            sym = ((rep.get("tokenMeta") or {}).get("symbol"))
            hits.append((sid, mint[:8], sym, g.name, g.state, g.reason))
print(f"reports: {len(reports)} (snapshot ids {min(reports)}-{max(reports)}), "
      f"programs {dict(collections.Counter(r.get('tokenProgram', '?')[:8] for _, r in reports.values()))}")
for k, v in sorted(states.items()):
    print(f"  {k[0]:22s} {k[1]:18s} {v}")
for h in hits:
    print("  HIT", h)

# decisions: live DB holds every cycle (1..N); archive copy is a subset
c = sqlite3.connect("data/memedesk.sqlite")
n = changed = 0
pos_hit = []
for did, cyc, mint, state, aux, det in c.execute(
        "SELECT id, cycle_id, mint, state, aux_snapshot_ids, details FROM decisions WHERE stage='scout'"):
    rids = [i for i in json.loads(aux or "[]") if i in reports]
    if not rids:
        continue
    n += 1
    old = [Gate(**g) for g in json.loads(det)["gates"]]
    new = combine(old + scout.token2022_gates(reports[rids[0]][1], cfg))
    if new.state != state:
        changed += 1
        print("  CHANGED", did, cyc, mint[:8], state, "->", new.state, new.name)
        pos_hit += c.execute("SELECT id, book, bot, status FROM positions WHERE mint=? AND book='desk'", (mint,)).fetchall()
print(f"scout decisions with a RugCheck report: {n}; changed: {changed}; desk positions affected: {len(set(pos_hit))}")
