#!/usr/bin/env bash
# Release step (CONTRIBUTING.md "Releases"): bring dz-dashboard's uv.lock up to the ff-pipeline
# version in this repo, through a PR to the dashboard's dev branch.
#
# The dashboard depends on ff-pipeline through an editable path source (../danger-zone), and uv
# records that package's version in the dashboard's uv.lock. Every version bump here therefore
# leaves the dashboard lock stale until it is re-locked; its CI (`uv lock --locked`) fails until then.
#
#   scripts/release/sync_dashboard_lock.sh --dry-run          # show what would change, touch nothing
#   scripts/release/sync_dashboard_lock.sh                    # branch, commit, push, open the PR
#   scripts/release/sync_dashboard_lock.sh --merge            # ...then wait for CI and merge it
#   ... --ai-model claude-opus-5-5                            # add the AI commit trailers
#
# Safe to re-run: it does nothing when the lock already matches. It refuses (exit 1) unless the
# dashboard is on a clean, up-to-date `dev`, and unless re-locking changes nothing but the
# ff-pipeline version line, so an unrelated dependency change can't slip into a release PR.
# Dashboard location: $DZ_DASHBOARD_DIR, default ../dz-dashboard next to this repo.
set -euo pipefail
dry_run="" merge="" ai_model=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) dry_run=1 ;;
    --merge) merge=1 ;;
    --ai-model) ai_model="${2:?--ai-model needs a model id}"; shift ;;
    *) echo "usage: $0 [--dry-run] [--merge] [--ai-model ID]" >&2; exit 2 ;;
  esac
  shift
done

repo="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"
dash="$(readlink -f "${DZ_DASHBOARD_DIR:-$repo/../dz-dashboard}")"
die() { echo "sync_dashboard_lock: $*" >&2; exit 1; }
[ -f "$dash/uv.lock" ] || die "no uv.lock in $dash (set DZ_DASHBOARD_DIR)"

want="$(python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' "$repo/pyproject.toml")"
locked_version() {
  python3 -c 'import sys, tomllib
pk = [p for p in tomllib.load(open(sys.argv[1], "rb"))["package"] if p["name"] == "ff-pipeline"]
print(pk[0].get("version", "") if pk else "")' "$dash/uv.lock"
}

cd "$dash"
[ "$(git branch --show-current)" = "dev" ] || die "dz-dashboard must be on dev (it is on '$(git branch --show-current)')"
[ -z "$(git status --porcelain)" ] || die "dz-dashboard has uncommitted changes; commit or stash them first"
git pull -q --ff-only

have="$(locked_version)"
if [ "$have" = "$want" ]; then
  echo "dz-dashboard uv.lock already has ff-pipeline $want; nothing to do"
  exit 0
fi
echo "dz-dashboard uv.lock: ff-pipeline $have -> $want"

uv lock -q
changed="$(git diff -U0 -- uv.lock | grep -E '^[-+][^-+]' || true)"
expected="$(printf -- '-version = "%s"\n+version = "%s"' "$have" "$want")"
if [ "$(git diff --name-only)" != "uv.lock" ] || [ "$changed" != "$expected" ]; then
  echo "re-locking changed more than the ff-pipeline version:" >&2
  git --no-pager diff --stat >&2
  echo "$changed" | head -40 >&2
  git checkout -q -- .
  die "lock left unchanged; re-lock the dashboard by hand and review the diff"
fi
uv lock --locked -q

if [ -n "$dry_run" ]; then
  git --no-pager diff -U0 -- uv.lock
  git checkout -q -- uv.lock
  echo "dry run: lock restored, nothing committed"
  exit 0
fi

branch="chore/ff-pipeline-$want-lock"
git switch -q -c "$branch"
msg="Lock ff-pipeline $want

danger-zone released ff-pipeline $want, and the editable path dependency's version is recorded
in uv.lock, so the lock was stale. Only the ff-pipeline version line changes."
if [ -n "$ai_model" ]; then
  msg="$msg

AI-Model: $ai_model
Prompted-By: ocmooch <https://github.com/ocmooch>
Reviewed-By: ocmooch <https://github.com/ocmooch>"
fi
git commit -q -m "$msg" -- uv.lock
git push -q -u origin "$branch"
url="$(gh pr create --base dev --head "$branch" --title "Lock ff-pipeline $want" \
  --body "Brings \`uv.lock\` up to ff-pipeline $want (danger-zone release). Only the \`ff-pipeline\` version line changes; opened by danger-zone \`scripts/release/sync_dashboard_lock.sh\`.")"
echo "opened $url"
if [ -n "$merge" ]; then
  sleep 10
  gh pr checks "$url" --watch --interval 20 >/dev/null || die "CI failed on $url; not merged"
  gh pr merge "$url" --merge --delete-branch >/dev/null
  git switch -q dev 2>/dev/null || true
  git pull -q --ff-only
  git branch -D -q "$branch" 2>/dev/null || true
  echo "merged $url; dz-dashboard dev now locks ff-pipeline $(locked_version)"
else
  git switch -q dev
  echo "left $branch for review; dz-dashboard is back on dev"
fi
