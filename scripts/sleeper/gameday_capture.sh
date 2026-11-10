#!/usr/bin/env bash
# Game-day players-directory capture (S7-D7): Sunday before the 1 PM ET kickoffs,
# after inactives, so injury / depth statuses are as of kickoff. Archives only
# /state/nfl and the players directory; `sleeper-league load` turns it into
# player_status_snapshots. Run by the Windows task `dz-sleeper-gameday`.
set -uo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
REPO="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"
cd "$REPO" || exit 1
mkdir -p data/logs
LOG="data/logs/sleeper_gameday-$(date -u +%Y%m%d).log"
{
  echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) sleeper-league capture --directory-only (branch $(git branch --show-current))"
  LOG_LEVEL=WARNING uv run ff-pipeline sleeper-league capture --directory-only 2>&1 | grep -v ' GET '
  rc=${PIPESTATUS[0]}
  echo "== capture exit $rc"
  exit "$rc"
} >>"$LOG" 2>&1
