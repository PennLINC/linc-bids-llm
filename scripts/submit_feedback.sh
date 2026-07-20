#!/usr/bin/env bash
# Submit your local feedback to the shared repo as a PR, so it aggregates with
# everyone else's and feeds the eval (eval/feedback_report.py, feedback_to_cases.py).
#
#   scripts/submit_feedback.sh
#
# Copies .feedback/feedback.jsonl -> eval/feedback/<you>.jsonl on a new branch
# and opens a PR. Your local .feedback/ is the cumulative log, so re-submitting
# refreshes your file. Needs `gh` authenticated; run from an up-to-date main.
set -euo pipefail

cd "$(dirname "$0")/.."
SRC=".feedback/feedback.jsonl"

# Resolve GitHub CLI explicitly (a bare `gh` can be shadowed on PATH).
find_gh() {
  if [ -n "${GH:-}" ]; then echo "$GH"; return 0; fi
  for c in gh /opt/homebrew/bin/gh /usr/local/bin/gh "$HOME/miniforge3/bin/gh"; do
    if command -v "$c" >/dev/null 2>&1 \
        && "$c" --version 2>/dev/null | grep -qi 'github.com/cli/cli'; then
      command -v "$c"; return 0
    fi
  done
  return 1
}

if [ ! -s "$SRC" ]; then
  echo "error: no feedback yet at $SRC — rate some answers in the app first." >&2
  exit 1
fi
GH="$(find_gh)" || {
  echo "error: GitHub CLI not found. Install it (brew install gh) or set GH=." >&2
  exit 1
}

USER_SLUG="$(git config user.name 2>/dev/null | tr '[:upper:] ' '[:lower:]-' \
             | tr -cd 'a-z0-9-')"
[ -n "$USER_SLUG" ] || USER_SLUG="$(echo "${USER:-maintainer}" | tr -cd 'a-z0-9-')"
START_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
BRANCH="feedback/${USER_SLUG}-$(date +%Y%m%d-%H%M%S)"
DEST="eval/feedback/${USER_SLUG}.jsonl"
N=$(wc -l < "$SRC" | tr -d ' ')

mkdir -p eval/feedback
cp "$SRC" "$DEST"
git checkout -b "$BRANCH"
git add "$DEST"
git commit -q -m "feedback: ${N} entries from ${USER_SLUG}"
git push -q -u origin "$BRANCH"
"$GH" pr create --fill \
  --title "Feedback: ${USER_SLUG} (${N} entries)" \
  --body "Battle-testing feedback from ${USER_SLUG}. Aggregate with \`python -m eval.feedback_report\`; promote failures with \`python -m eval.feedback_to_cases\`."
git checkout -q "$START_BRANCH"
echo "opened PR from $BRANCH ($N entries) and returned to $START_BRANCH"
