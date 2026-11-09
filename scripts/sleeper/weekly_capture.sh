#!/usr/bin/env bash
# Weekly Sleeper league capture (S2, decision D-S2-9). Windows Task Scheduler runs this
# (task "dz-sleeper-capture", Wed 06:00) through wsl.exe with no arguments; it is kept
# separate from dz-vault-sync so a Sleeper outage can never fail the vault job.
#
# Captures the newest league in the SLEEPER_LEAGUE_ID chain (all weeks to date), then
# re-runs the legacy verify. Log: data/logs/sleeper_capture-<UTC date>.log. Exit code is
# the capture's (1 = some request failed after retries, or the preflight refused).
set -uo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
REPO="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"
cd "$REPO" || exit 1
mkdir -p data/logs
LOG="data/logs/sleeper_capture-$(date -u +%Y%m%d).log"
{
  echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) sleeper-league capture (branch $(git branch --show-current))"
  LOG_LEVEL=WARNING uv run ff-pipeline sleeper-league capture 2>&1 | grep -v ' GET '
  rc=${PIPESTATUS[0]}
  echo "== capture exit $rc"
  uv run ff-pipeline legacy verify --quick 2>&1 | tail -1
  exit "$rc"
} >>"$LOG" 2>&1
