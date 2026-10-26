#!/bin/bash
# Box helper: fetch the latest `state` branch from GitHub (the primary runner) and install it
# as data/memedesk.sqlite for READ-ONLY consumers (sheet sync, scorecard, bots).
#  - atomic replace (download -> gunzip -> integrity check -> mv)
#  - refreshes data/report_latest.txt and data/cron_actions.log from the runner
#  - merges the retained raw snapshots into data/archive/snapshots_archive.sqlite so the box
#    keeps full snapshot history (runner keeps ~6 h; pull at least every ~5 h to avoid gaps)
# Refuses to run while the retired box loop is running (that would mean two writers).
# Public repo: no credentials needed.
set -euo pipefail
cd "$(dirname "$0")"
REPO_URL=${MEMEDESK_STATE_REPO:-https://github.com/manarinico/kastor-memedesk.git}
PY=.venv/bin/python
if pgrep -f 'run_loop.sh' >/dev/null; then
  echo "pull_state: box run_loop.sh is running - refusing (GitHub Actions is the only writer)" >&2; exit 3
fi
CACHE=data/.state_git
[ -d "$CACHE" ] || git init -q --bare "$CACHE"
git -C "$CACHE" fetch -q --depth 1 "$REPO_URL" +refs/heads/state:refs/heads/state
TMP=$(mktemp -d data/.pull.XXXXXX); trap 'rm -rf "$TMP"' EXIT
git -C "$CACHE" cat-file blob state:memedesk.sqlite.gz | gunzip > "$TMP/memedesk.sqlite"
"$PY" scripts/state_tool.py check "$TMP/memedesk.sqlite" >/dev/null
git -C "$CACHE" cat-file blob state:report_latest.txt > "$TMP/report_latest.txt" 2>/dev/null || true
git -C "$CACHE" cat-file blob state:cron.log > "$TMP/cron_actions.log" 2>/dev/null || true
git -C "$CACHE" cat-file blob state:STATE.json > "$TMP/STATE.json" 2>/dev/null || true
# archive merge (append-only; ids are globally unique because the newest row is never pruned)
mkdir -p data/archive
if [ -f data/archive/snapshots_archive.sqlite ]; then
  "$PY" - "$TMP/memedesk.sqlite" <<'PYEOF'
import sqlite3, sys
a = sqlite3.connect("data/archive/snapshots_archive.sqlite")
a.execute("ATTACH ? AS s", (sys.argv[1],))
n = a.execute("INSERT OR IGNORE INTO main.snapshots SELECT * FROM s.snapshots").rowcount
a.commit(); a.execute("DETACH s"); a.close()
print(f"archive: +{n} snapshots")
PYEOF
fi
rm -f data/memedesk.sqlite-wal data/memedesk.sqlite-shm
mv -f "$TMP/memedesk.sqlite" data/memedesk.sqlite
[ -s "$TMP/report_latest.txt" ] && mv -f "$TMP/report_latest.txt" data/report_latest.txt
[ -s "$TMP/cron_actions.log" ] && mv -f "$TMP/cron_actions.log" data/cron_actions.log
[ -s "$TMP/STATE.json" ] && mv -f "$TMP/STATE.json" data/STATE.json
echo "pull_state: installed $("$PY" scripts/state_tool.py info data/memedesk.sqlite)"
