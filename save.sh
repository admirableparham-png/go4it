#!/usr/bin/env bash
#
# go4it — end-of-day save.
# Commits everything and pushes to GitHub, with a clear status report so you
# always know the day landed safely. Run it whenever you like:
#
#     ./save.sh            (or:  make save)
#
set -euo pipefail

cd "$(dirname "$0")"

DATE="$(date '+%Y-%m-%d %H:%M')"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"

echo "──────────────────────────────────────────────"
echo "  go4it daily save · $DATE · branch: $BRANCH"
echo "──────────────────────────────────────────────"

# Guard: a detached HEAD would commit onto a nameless ref and fail to push,
# stranding the day's work. Stop before committing and tell the user how to fix.
if [[ "$BRANCH" == "HEAD" ]]; then
  echo "✗ Detached HEAD — you are not on a branch, so a save would be lost."
  echo "  Fix:  git switch main     (or:  git switch -c <branch-name>)"
  echo "  then re-run ./save.sh"
  exit 1
fi

if [[ -z "$(git status --porcelain)" ]]; then
  echo "✓ Nothing changed since last save. Working tree is clean."
else
  git add -A
  FILES="$(git diff --cached --name-only | wc -l | tr -d ' ')"
  MSG="save: end of day $DATE (${FILES} files)"
  git commit -m "$MSG" >/dev/null
  echo "✓ Committed — $MSG"
fi

# Push. Handle three cases cleanly: upstream set, remote exists but no upstream,
# and no remote at all (save locally instead of aborting with a raw git error).
if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  git push
  echo "✓ Pushed to $(git remote get-url origin)"
elif git remote get-url origin >/dev/null 2>&1; then
  git push -u origin "$BRANCH"
  echo "✓ Pushed to $(git remote get-url origin)"
else
  echo "✓ Committed locally. No 'origin' remote yet — add one to enable push:"
  echo "    git remote add origin <url>"
  exit 0
fi

echo ""
echo "Recent history:"
git --no-pager log --oneline -5
echo ""
echo "✓ Everything is safe on GitHub. Good work today."
