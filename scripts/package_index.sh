#!/usr/bin/env bash
# Package the local index/ into a tarball and (optionally) publish it as a
# GitHub Release asset, so maintainer-testers can skip the ~16-min harvest.
#
# The index is a build artifact, not source: it is gitignored and shipped as a
# release asset — release assets don't bloat git history and can be overwritten
# or deleted anytime (unlike a committed file, which lives in history forever).
#
#   scripts/package_index.sh                 # build dist/index.tgz only
#   scripts/package_index.sh --upload [tag]  # build + publish (default tag: index-latest)
#
# Publishing needs the `gh` CLI authenticated (`gh auth login`). --clobber
# replaces the asset in place, so re-running after a re-ingest just updates it.
set -euo pipefail

cd "$(dirname "$0")/.."
TARBALL="dist/index.tgz"

# Resolve GitHub CLI explicitly: a bare `gh` can be shadowed on PATH by an
# unrelated tool of the same name. Real GitHub CLI prints a github.com/cli/cli
# URL in --version. Override with GH=/path/to/gh if needed.
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

if [ ! -f index/manifest.json ]; then
  echo "error: no index/manifest.json — build the index first: python -m src.ingest" >&2
  exit 1
fi

mkdir -p dist
tar czf "$TARBALL" index/
echo "built $TARBALL ($(du -h "$TARBALL" | cut -f1))"
python3 - <<'PY'
import json
m = json.load(open("index/manifest.json"))
print(f"  embedding model: {m.get('embedding_model')}")
print(f"  built_at:        {m.get('built_at')}")
print(f"  chunks:          {m.get('chunks')}")
PY

if [ "${1:-}" = "--upload" ]; then
  TAG="${2:-index-latest}"
  TITLE="Prebuilt index ($(date -u +%Y-%m-%d))"
  NOTES="Prebuilt bids-assistant index for maintainer testing. Fetch with scripts/fetch_index.sh. This is a build artifact and may be replaced or removed at any time."
  GH="$(find_gh)" || {
    echo "error: GitHub CLI not found. Install it (brew install gh) or set" >&2
    echo "       GH=/path/to/gh. A different 'gh' may be shadowing it on PATH." >&2
    exit 1
  }
  echo "using GitHub CLI: $GH"
  if ! "$GH" release view "$TAG" >/dev/null 2>&1; then
    echo "creating release '$TAG'..."
    "$GH" release create "$TAG" --title "$TITLE" --notes "$NOTES" --prerelease
  fi
  "$GH" release upload "$TAG" "$TARBALL" --clobber
  echo "published $TARBALL to release '$TAG'"
else
  echo
  echo "to publish:  scripts/package_index.sh --upload [tag]"
fi
