#!/bin/bash
# Build the slim state (gzipped DB minus old raw snapshots + report + log tail + STATE.json)
# and force-push it as the single commit of the orphan branch `state`.
#   scripts/push_state.sh <data_dir> <lease_sha|NONE>
# lease: the state commit this writer started from. The push is refused if `state` moved
# since (another writer) - fail closed rather than clobber. NONE = branch must not exist yet.
# Env: PYTHON (default python), STATE_HOST, KEEP_HOURS (default 6), REMOTE (default origin).
set -euo pipefail
DATA=$(cd "$1" && pwd); LEASE=${2:?lease sha or NONE}
PY=${PYTHON:-python}; REMOTE=${REMOTE:-origin}
ROOT=$(git rev-parse --show-toplevel); GITDIR=$(git rev-parse --absolute-git-dir)
OUT=$(mktemp -d); trap 'rm -rf "$OUT"' EXIT
mkdir -p "$OUT/tree"
"$PY" "$ROOT/scripts/state_tool.py" slim "$DATA/memedesk.sqlite" "$OUT/tree/memedesk.sqlite" --keep-hours "${KEEP_HOURS:-6}"
INFO=$("$PY" "$ROOT/scripts/state_tool.py" info "$OUT/tree/memedesk.sqlite")
RAW_SHA=$(sha256sum "$OUT/tree/memedesk.sqlite" | cut -d' ' -f1)
gzip -9 -n "$OUT/tree/memedesk.sqlite"
[ -f "$DATA/report_latest.txt" ] && cp "$DATA/report_latest.txt" "$OUT/tree/report_latest.txt"
[ -f "$DATA/cron.log" ] && tail -n 5000 "$DATA/cron.log" > "$OUT/tree/cron.log"
"$PY" - "$OUT/tree/STATE.json" "$INFO" "$RAW_SHA" <<'PYEOF'
import json, os, sys, time
info = json.loads(sys.argv[2])
info.update({"db_sha256": sys.argv[3], "host": os.environ.get("STATE_HOST", "unknown"),
             "pushed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "run_url": os.environ.get("RUN_URL", ""), "paper_only": True})
open(sys.argv[1], "w").write(json.dumps(info, indent=1) + "\n")
PYEOF
export GIT_INDEX_FILE="$OUT/index"
git --git-dir="$GITDIR" --work-tree="$OUT/tree" add -A "$OUT/tree"
TREE=$(git --git-dir="$GITDIR" write-tree)
MSG="state: cycle $(echo "$INFO" | "$PY" -c 'import json,sys;d=json.load(sys.stdin);print(d["last_cycle"], d["status"])') from ${STATE_HOST:-unknown}"
COMMIT=$(GIT_AUTHOR_NAME=memedesk-state GIT_AUTHOR_EMAIL=state@memedesk.invalid \
         GIT_COMMITTER_NAME=memedesk-state GIT_COMMITTER_EMAIL=state@memedesk.invalid \
         git --git-dir="$GITDIR" commit-tree "$TREE" -m "$MSG")
[ "$LEASE" = NONE ] && LEASE=""
git --git-dir="$GITDIR" push --force-with-lease="refs/heads/state:$LEASE" "$REMOTE" "$COMMIT:refs/heads/state"
echo "pushed state $COMMIT ($MSG); gz $(stat -c %s "$OUT/tree/memedesk.sqlite.gz") bytes"
