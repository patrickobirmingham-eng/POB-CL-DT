#!/usr/bin/env bash
# Publish bot-generated files (dashboard, state, logs) to main.
#
# Usage: scripts/publish_files.sh "<commit message>" FILE [FILE...]
#
# Instead of merging (which can stop half-way on a conflict in a generated
# file like docs/index.html and leave the checkout stuck, so every later push
# fails), this takes the LATEST origin/main and commits only the listed files
# on top of it, from this job's working tree. Code and settings are never
# touched: only the listed files are added. Safe to call repeatedly.
set -u
msg="$1"; shift

git config user.name "orb-trading-bot"
git config user.email "actions@users.noreply.github.com"
# Snapshot the files to publish FIRST: aborting a half-finished rebase/merge
# (left behind by an older version of the bot) resets the working tree and
# would otherwise throw away the newest dashboard/state written since.
snap="$(mktemp -d)"
trap 'rm -rf "$snap"' EXIT
for f in "$@"; do
  [ -e "$f" ] && mkdir -p "$snap/$(dirname "$f")" && cp -p -- "$f" "$snap/$f"
done
git rebase --abort >/dev/null 2>&1 || true
git merge --abort >/dev/null 2>&1 || true

for i in 1 2 3 4 5; do
  if ! git fetch -q origin main; then
    echo "publish: fetch failed (attempt $i)"; sleep $((i * 2)); continue
  fi
  # Index + HEAD = latest main; working-tree files (ours) are left as they are.
  git reset -q --mixed origin/main
  for f in "$@"; do
    if [ -e "$snap/$f" ]; then
      mkdir -p "$(dirname "$f")" && cp -p -- "$snap/$f" "$f"
      git add -- "$f"
    fi
  done
  if git diff --cached --quiet; then
    echo "publish: nothing changed."
    exit 0
  fi
  git commit -q -m "$msg"
  if git push -q origin HEAD:main; then
    echo "publish: pushed ($msg)."
    exit 0
  fi
  echo "publish: push rejected (attempt $i); retrying on the latest main..."
  sleep $((i * 2))
done
echo "publish: failed after 5 attempts."
exit 1
