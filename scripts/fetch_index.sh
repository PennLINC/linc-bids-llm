#!/usr/bin/env bash
# Fetch the prebuilt index from a GitHub Release and unpack it into index/,
# so you can run the app without the ~16-min harvest.
#
#   scripts/fetch_index.sh [tag]        # default tag: index-latest
#
# Uses `gh` when available (needed for a private repo). If gh cannot download
# — an installed but not logged-in gh refuses even a public repo (exit 4), and
# during a re-publish the asset is briefly gone — it falls back to a plain curl
# of the public asset URL, so a server with no (or an idle) `gh` still works.
#
# The live index/ is replaced only after the new one is downloaded, unpacked
# beside it and its manifest checks out; whatever fails before that leaves
# index/ exactly as it was. The replaced index/ is kept as index.bak.<epoch>,
# and only that one (each is ~150 MB; older backups are removed).
#
# Shares .refresh.lock with refresh.sh and deploy.sh (one of them at a time may
# touch index/). Taken non-blocking: if a refresh or deploy is running this
# exits 75 without touching anything. deploy.sh calls this with the lock
# already held and says so with REFRESH_LOCK_HELD=1. No flock (macOS): unlocked.
set -euo pipefail

cd "$(dirname "$0")/.."
TAG="${1:-index-latest}"
TARBALL="dist/index.tgz"
TMP_TARBALL="$TARBALL.tmp"
UNPACK="index.fetch.$$"

if [ "${REFRESH_LOCK_HELD:-}" != "1" ] && command -v flock >/dev/null 2>&1; then
  exec 9>.refresh.lock
  if ! flock -n 9; then
    echo "error: a refresh or deploy is running (holds .refresh.lock) — wait for it, then retry" >&2
    exit 75
  fi
fi

# However this ends, only this run's transient files go; index/ is never one.
cleanup() { rm -rf "$UNPACK" "$TMP_TARBALL"; }
trap cleanup EXIT

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

# 1. download, under a temporary name: index/ is not touched yet.
mkdir -p dist
rm -f "$TMP_TARBALL"
got=""
if GH="$(find_gh)"; then
  echo "downloading index.tgz from release '$TAG' (using $GH)..."
  if "$GH" release download "$TAG" --pattern index.tgz --output "$TMP_TARBALL" --clobber; then
    got=gh
  else
    echo "gh could not download it (not logged in? asset being re-published?); trying the public URL..."
  fi
fi
if [ -z "$got" ]; then
  URL="https://github.com/$(repo_slug)/releases/download/${TAG}/index.tgz"
  echo "curl-ing public asset: $URL"
  curl -fL --retry 3 -o "$TMP_TARBALL" "$URL" || {
    echo "error: download failed; index/ is untouched. If the repo is private, log" >&2
    echo "       gh in (gh auth login); else check the release tag '$TAG' and asset name." >&2
    exit 1
  }
fi

# 2. unpack beside index/, not over it, and check the manifest before trusting it.
mkdir "$UNPACK"
tar xzf "$TMP_TARBALL" -C "$UNPACK" || {    # the tarball holds index/...
  echo "error: could not unpack $TMP_TARBALL; index/ is untouched." >&2
  exit 1
}
if [ ! -f "$UNPACK/index/manifest.json" ]; then
  echo "error: the tarball has no index/manifest.json; index/ is untouched." >&2
  exit 1
fi
echo "downloaded index:"
python3 - "$UNPACK/index/manifest.json" <<'PY'
import json, sys
try:
    m = json.load(open(sys.argv[1]))
    chunks = m.get("chunks")
    if not (isinstance(chunks, dict) and chunks and sum(chunks.values()) > 0):
        raise ValueError(f"no chunks in manifest: {chunks!r}")
except Exception as e:                      # not JSON, not a dict, empty
    sys.exit(f"error: downloaded index rejected ({e}); index/ is untouched.")
print(f"  embedding model: {m.get('embedding_model')}")
print(f"  built_at:        {m.get('built_at')}")
print(f"  chunks:          {chunks}")
PY

# 3. swap: the old index/ becomes index.bak.<epoch>, the new one takes its place.
BACKUP=""
if [ -e index ] || [ -L index ]; then
  BACKUP="index.bak.$(date +%s)"
  [ ! -e "$BACKUP" ] || BACKUP="$BACKUP.$$"
  echo "existing index/ -> $BACKUP/"
  mv index "$BACKUP"
fi
if ! mv "$UNPACK/index" index; then
  [ -z "$BACKUP" ] || mv "$BACKUP" index
  echo "error: could not move the new index into place; the old index/ is back." >&2
  exit 1
fi
rmdir "$UNPACK"
mv "$TMP_TARBALL" "$TARBALL"
echo "unpacked index/ (tarball kept at $TARBALL)"

# 4. keep one backup, the index just replaced; older ones only eat disk.
shopt -s nullglob; backups=(index.bak.*); shopt -u nullglob
if [ "${#backups[@]}" -gt 1 ]; then
  for old in "${backups[@]:0:${#backups[@]}-1}"; do
    rm -rf "$old"; echo "removed older backup $old/"
  done
fi
[ -z "$BACKUP" ] || echo "kept one backup: $BACKUP/ (rm -rf it once the new index checks out)"

echo
echo "next: python -m src.checkouts   # clone code for the agent path (~2 min)"
echo "then: streamlit run app.py"
