#!/usr/bin/env bash
# Show, hide or check the console windows of the weekly danger-zone Windows tasks
# (dz-sleeper-capture, dz-vault-sync). Run from WSL:
#
#   scripts/windows/task_windows.sh status    # mode, command, last run/result of each task
#   scripts/windows/task_windows.sh hidden    # no window (watch progress in data/logs/)
#   scripts/windows/task_windows.sh visible   # a window with live progress; don't close it
#
# See docs/RUNBOOK.md "Window mode".
set -euo pipefail
case "${1:-}" in
  status) mode=Status ;;
  hidden) mode=Hidden ;;
  visible) mode=Visible ;;
  *) echo "usage: $0 status|hidden|visible" >&2; exit 2 ;;
esac
ps1="$(wslpath -w "$(dirname "$(readlink -f "$0")")/task_windows.ps1")"
exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$ps1" -Mode "$mode"
