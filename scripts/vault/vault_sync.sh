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
#
# In a console (the task in Visible mode, or a manual run) every wait is announced, a
# "still waiting" line prints every 30 s, and the window stays open 30 s at the end with the
# result. Closing the window mid-run kills the job (RUNBOOK "Window mode").
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
  console=""
  [ -t 1 ] && console=1
  say() { [ -n "$console" ] && echo "$*"; return 0; }
  note() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') INFO wait: $*" >>"$logs/vault_sync-$(date +%F).log"
    say "wait: $*"
  }
  hb=""
  if [ -n "$console" ]; then
    echo "dz-vault-sync: weekly off-site vault backup (danger-zone, scripts/vault/vault_sync.sh)"
    echo "Running: up to ~35 min if it waits for the capture. DON'T CLOSE THIS WINDOW; it closes itself when done."
    echo "Log: $(readlink -f "$logs")/vault_sync-$(date -u +%F).log"
    echo
    ( t0=$SECONDS; while sleep 30; do
        printf '   ... still waiting (%dm%02ds)\n' $(((SECONDS - t0) / 60)) $(((SECONDS - t0) % 60))
      done ) &
    hb=$!
  fi
  say "Waiting ${DZ_VAULT_GRACE_SECONDS:-120} s for the weekly capture to start (they often launch together)..."
  sleep "${DZ_VAULT_GRACE_SECONDS:-120}"
  touch "$logs/weekly_capture.lock"
  if flock -n "$logs/weekly_capture.lock" true; then
    note "no capture running"
  else
    say "The weekly capture is running; waiting for it to finish (up to $(( ${DZ_VAULT_WAIT_SECONDS:-1800} / 60 )) min)..."
    if flock -w "${DZ_VAULT_WAIT_SECONDS:-1800}" "$logs/weekly_capture.lock" true; then
      note "waited for the weekly capture to finish"
    else
      note "capture still running after the wait limit; snapshotting anyway"
    fi
  fi
  [ -n "$hb" ] && kill "$hb" 2>/dev/null
  say "Building, uploading and verifying the snapshot..."
  rc=0
  python3 "$here/vault_sync.py" || rc=$?
  if [ -n "$console" ]; then
    echo
    if [ "$rc" -eq 0 ]; then echo "Finished: ok. This window closes in 30 s."
    else echo "Finished: FAILED (exit $rc). This window closes in 30 s."; fi
    sleep 30
  fi
  exit "$rc"
fi
exec python3 "$here/vault_sync.py" "$@"
