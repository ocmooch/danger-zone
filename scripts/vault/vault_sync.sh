#!/usr/bin/env bash
# Entry point for the off-site vault sync. Windows Task Scheduler runs it with no arguments
# (Wed 07:00, from the ~/danger-zone-main worktree, an hour after the weekly capture). See
# vault_sync.py --help and docs/sleeper/05_VAULT_OFFSITE_SYNC.md.
#
# The no-argument weekly run is main-only, like the capture: on any other branch it refuses
# with exit 3. Subcommands (build-seal, pack-seal, sync, ...) run on any branch, because
# `legacy seal-season` calls them from ~/danger-zone.
set -euo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
here="$(dirname "$(readlink -f "$0")")"
if [ "$#" -eq 0 ]; then
  branch="$(git -C "$here" branch --show-current)"
  if [ "$branch" != "main" ]; then
    echo "vault_sync: refusing the weekly run on '${branch:-detached HEAD}'; it runs only on main (docs/RUNBOOK.md \"The main copy\")" >&2
    exit 3
  fi
fi
exec python3 "$here/vault_sync.py" "$@"
