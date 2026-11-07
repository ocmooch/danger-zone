#!/usr/bin/env bash
# Entry point for the off-site vault sync (Windows Task Scheduler runs this weekly
# with no arguments). See vault_sync.py --help and docs/sleeper/05_VAULT_OFFSITE_SYNC.md.
set -euo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
exec python3 "$(dirname "$(readlink -f "$0")")/vault_sync.py" "$@"
