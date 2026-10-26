#!/bin/bash
# Paper-trading loop: one cycle, then sleep 20 min, forever.
# - flock -n -E 75: skip (and log it) if another cycle holds data/.lock
# - timeout -k 30 600: a cycle can never run longer than 10 min (SIGTERM, SIGKILL 30s later)
# - python appends its own timestamped result/failure lines to data/cron.log; stderr
#   (tracebacks) goes there too; this script logs start/end with exit code around each cycle.
cd /workspace/memedesk
# RETIRED 2026-10-26 11:19:25 EDT: GitHub Actions (manarinico/kastor-memedesk) is the only writer.
# Running this loop as well would fork the desk state. Override only on purpose.
if [ "${MEMEDESK_ALLOW_BOX_LOOP:-0}" != 1 ]; then
  echo "run_loop.sh is retired: the desk runs on GitHub Actions. Use ./pull_state.sh to read state." >&2
  exit 3
fi
while true; do
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] loop: cycle start (loop pid $$, uptime $(cut -d' ' -f1 /proc/uptime)s)" >> data/cron.log
  flock -n -E 75 data/.lock timeout -k 30 600 .venv/bin/python -m memedesk run_cycle >/dev/null 2>> data/cron.log
  rc=$?
  case $rc in
    0)   note="ok" ;;
    75)  note="SKIPPED: data/.lock held by another cycle" ;;
    124) note="KILLED: hit the 600s timeout" ;;
    137) note="KILLED: SIGKILL after timeout" ;;
    *)   note="FAILED" ;;
  esac
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] loop: cycle end rc=$rc $note" >> data/cron.log
  .venv/bin/python -m memedesk report > data/report_latest.txt.tmp 2>> data/cron.log && mv data/report_latest.txt.tmp data/report_latest.txt
  sleep 1200
done
