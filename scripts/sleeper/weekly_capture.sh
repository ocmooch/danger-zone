#!/usr/bin/env bash
# The weekly Sleeper job (S2, S9-D1..D3). Windows Task Scheduler runs this (task
# "dz-sleeper-capture", Wed 06:00 ET: after Monday night and Tuesday's stat corrections)
# through wsl.exe with no arguments; it is kept separate from dz-vault-sync so a Sleeper
# outage can never fail the vault job.
#
#   1. capture the newest league in the SLEEPER_LEAGUE_ID chain (every week to date, so a
#      later stat correction lands next week; follows a renewed league, S9-D4)
#   2. `sleeper-league update --if-enabled`: load + score, only once the go-live set
#      SLEEPER_AUTO_LOAD=true (S9-D2); a no-op before that
#   3. `backfill-projections --current-season`: Sleeper projections for the upcoming week
#      (re-fetched each run until it is played) plus any earlier week still missing; only
#      weeks with loaded matchups count, so it is a no-op before the go-live
#   4. full `legacy verify`
#
# Runs only on `main` (the stable, released code): Task Scheduler points at the dedicated
# ~/danger-zone-main worktree, which shares ~/danger-zone's data/ and .env through links
# (docs/RUNBOOK.md "The main copy"). On any other branch it logs a refusal and exits 3.
#
# Holds data/logs/weekly_capture.lock while it runs, so the weekly vault sync can wait for it
# (both fire together at login when the PC was off at 06:00 and 07:00). A second capture
# started while one is running exits 4.
#
# Stops at the first failing step (no silent retries; next Wednesday re-runs everything
# idempotently). Log: data/logs/sleeper_capture-<UTC date>.log. Exit code != 0 shows as
# "Last Result" in Task Scheduler.
#
# In a console (the task in Visible mode, or a manual run) the log is also shown live, with a
# "still running" line every 30 s, and the window stays open 30 s at the end with the result.
# Closing the window mid-run kills the job (RUNBOOK "Window mode": scripts/windows/task_windows.sh).
set -uo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
REPO="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"
cd "$REPO" || exit 1
mkdir -p data/logs
LOG="data/logs/sleeper_capture-$(date -u +%Y%m%d).log"
export PYTHONUNBUFFERED=1  # stream each step's output through tee as it happens
console=""
hb=""
if [ -t 1 ]; then
  console=1
  echo "dz-sleeper-capture: weekly Sleeper job (danger-zone, scripts/sleeper/weekly_capture.sh)"
  echo "Running: usually 3-10 min. DON'T CLOSE THIS WINDOW; it closes itself when done."
  echo "Log: $REPO/$LOG"
  echo
  # Console-only heartbeat (not logged) so long silent steps don't look finished.
  ( t0=$SECONDS; while sleep 30; do
      printf '   ... still running (%dm%02ds)\n' $(((SECONDS - t0) / 60)) $(((SECONDS - t0) % 60))
    done ) &
  hb=$!
fi
{
  branch="$(git branch --show-current)"
  echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) weekly Sleeper job (branch $branch, $(git rev-parse --short HEAD))"
  if [ "$branch" != "main" ]; then
    echo "== refusing: captures run only on main, not '${branch:-detached HEAD}' (see docs/RUNBOOK.md \"The main copy\")"
    exit 3
  fi
  exec 9>data/logs/weekly_capture.lock
  if ! flock -n 9; then
    echo "== another weekly capture is already running; exiting"
    exit 4
  fi
  echo "== step 1/4: capture"
  LOG_LEVEL=WARNING uv run ff-pipeline sleeper-league capture 2>&1 | grep --line-buffered -v ' GET '
  rc=${PIPESTATUS[0]}
  echo "== capture exit $rc"
  if [ "$rc" -eq 0 ]; then
    echo "== step 2/4: update (load + score; each sub-step prints when it finishes)"
    LOG_LEVEL=WARNING uv run ff-pipeline sleeper-league update --if-enabled 2>&1
    rc=${PIPESTATUS[0]}
    echo "== update exit $rc"
  fi
  if [ "$rc" -eq 0 ]; then
    echo "== step 3/4: projections"
    LOG_LEVEL=WARNING uv run ff-pipeline backfill-projections --current-season 2>&1
    rc=${PIPESTATUS[0]}
    echo "== projections exit $rc"
  fi
  echo "== step 4/4: legacy verify"
  uv run ff-pipeline legacy verify 2>&1 | tail -1
  vrc=${PIPESTATUS[0]}
  echo "== verify exit $vrc"
  [ "$rc" -eq 0 ] && rc=$vrc
  if [ "$rc" -eq 0 ]; then echo "== done: ok"; else echo "== done: FAILED (exit $rc)"; fi
  exit "$rc"
} 2>&1 | tee -a "$LOG"
rc=${PIPESTATUS[0]}
[ -n "$hb" ] && kill "$hb" 2>/dev/null
if [ -n "$console" ]; then
  # The lock is already released here, so this pause never delays the vault sync.
  echo
  echo "Finished (exit $rc). This window closes in 30 s."
  sleep 30
fi
exit "$rc"
