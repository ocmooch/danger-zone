#!/usr/bin/env bash
# Entry point for the off-site vault sync. Windows Task Scheduler runs it with no arguments
# (Wed 07:00, from the ~/danger-zone-main worktree, an hour after the weekly capture). See
# vault_sync.py --help and docs/sleeper/05_VAULT_OFFSITE_SYNC.md.
#
# The no-argument weekly run is main-only, like the capture: on any other branch it refuses
# with exit 3. Subcommands (build-seal, pack-seal, sync, ...) run on any branch, because
# `legacy seal-season` calls them from ~/danger-zone.
#
# The weekly run also waits for a running capture (data/logs/weekly_capture.lock), so its
# snapshot includes that week's data even when a missed 06:00 and 07:00 fire together at
# login. It first allows a short grace for the capture to start, then waits up to 30 min (the
# capture's own time limit), then snapshots anyway.
set -euo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
here="$(dirname "$(readlink -f "$0")")"
if [ "$#" -eq 0 ]; then
  branch="$(git -C "$here" branch --show-current)"
  if [ "$branch" != "main" ]; then
    echo "vault_sync: refusing the weekly run on '${branch:-detached HEAD}'; it runs only on main (docs/RUNBOOK.md \"The main copy\")" >&2
    exit 3
  fi
  logs="$here/../../data/logs"
  note() { echo "$(date '+%Y-%m-%d %H:%M:%S') INFO wait: $*" >>"$logs/vault_sync-$(date +%F).log"; }
  sleep "${DZ_VAULT_GRACE_SECONDS:-120}"
  touch "$logs/weekly_capture.lock"
  if flock -n "$logs/weekly_capture.lock" true; then
    note "no capture running"
  elif flock -w "${DZ_VAULT_WAIT_SECONDS:-1800}" "$logs/weekly_capture.lock" true; then
    note "waited for the weekly capture to finish"
  else
    note "capture still running after the wait limit; snapshotting anyway"
  fi
fi
exec python3 "$here/vault_sync.py" "$@"
