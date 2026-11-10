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
#   3. full `legacy verify`
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
set -uo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
REPO="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"
cd "$REPO" || exit 1
mkdir -p data/logs
LOG="data/logs/sleeper_capture-$(date -u +%Y%m%d).log"
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
  LOG_LEVEL=WARNING uv run ff-pipeline sleeper-league capture 2>&1 | grep -v ' GET '
  rc=${PIPESTATUS[0]}
  echo "== capture exit $rc"
  if [ "$rc" -eq 0 ]; then
    LOG_LEVEL=WARNING uv run ff-pipeline sleeper-league update --if-enabled 2>&1
    rc=${PIPESTATUS[0]}
    echo "== update exit $rc"
  fi
  uv run ff-pipeline legacy verify 2>&1 | tail -1
  vrc=${PIPESTATUS[0]}
  echo "== verify exit $vrc"
  [ "$rc" -eq 0 ] && rc=$vrc
  exit "$rc"
} >>"$LOG" 2>&1
