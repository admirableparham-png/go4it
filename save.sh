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

if [[ -z "$(git status --porcelain)" ]]; then
  echo "✓ Nothing changed since last save. Working tree is clean."
else
  git add -A
  FILES="$(git diff --cached --name-only | wc -l | tr -d ' ')"
  MSG="save: end of day $DATE (${FILES} files)"
  git commit -m "$MSG" >/dev/null
  echo "✓ Committed — $MSG"
fi

# Push. On the very first push, set the upstream automatically.
if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  git push
else
  git push -u origin "$BRANCH"
fi

echo "✓ Pushed to $(git remote get-url origin 2>/dev/null || echo '(no remote yet)')"
echo ""
echo "Recent history:"
git --no-pager log --oneline -5
echo ""
echo "✓ Everything is safe on GitHub. Good work today."
