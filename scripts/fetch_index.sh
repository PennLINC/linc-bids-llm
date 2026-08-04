#!/usr/bin/env bash
# Fetch the prebuilt index from a GitHub Release and unpack it into index/,
# so you can run the app without the ~16-min harvest.
#
#   scripts/fetch_index.sh [tag]        # default tag: index-latest
#
# Uses `gh` when available (needed for a private repo). On a PUBLIC repo it
# falls back to a plain curl of the release asset — so a server with no `gh`
# still works. An existing index/ is moved aside to index.bak.<timestamp>.
set -euo pipefail

cd "$(dirname "$0")/.."
TAG="${1:-index-latest}"

# Resolve GitHub CLI explicitly (a bare `gh` can be shadowed on PATH). Real
# GitHub CLI prints a github.com/cli/cli URL in --version. Override with GH=.
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

# owner/repo from the origin remote, for the public curl fallback.
repo_slug() {
  git config --get remote.origin.url \
    | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##'
}

if [ -d index ] && [ -f index/manifest.json ]; then
  BACKUP="index.bak.$(date +%s)"
  echo "existing index/ -> $BACKUP"
  mv index "$BACKUP"
fi

mkdir -p dist
if GH="$(find_gh)"; then
  echo "downloading index.tgz from release '$TAG' (using $GH)..."
  "$GH" release download "$TAG" --pattern index.tgz --dir dist --clobber
else
  URL="https://github.com/$(repo_slug)/releases/download/${TAG}/index.tgz"
  echo "no gh CLI; curl-ing public asset: $URL"
  curl -fL --retry 3 -o dist/index.tgz "$URL" || {
    echo "error: download failed. If the repo is private, install gh; else" >&2
    echo "       check the release tag '$TAG' and asset name." >&2
    exit 1
  }
fi
tar xzf dist/index.tgz    # recreates index/ at the repo root
echo "unpacked index/:"
python3 - <<'PY'
import json
m = json.load(open("index/manifest.json"))
print(f"  embedding model: {m.get('embedding_model')}")
print(f"  built_at:        {m.get('built_at')}")
print(f"  chunks:          {m.get('chunks')}")
PY

echo
echo "next: python -m src.checkouts   # clone code for the agent path (~2 min)"
echo "then: streamlit run app.py"
