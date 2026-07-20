#!/usr/bin/env bash
# Fetch the prebuilt index from a GitHub Release and unpack it into index/,
# so you can run the app without the ~16-min harvest.
#
#   scripts/fetch_index.sh [tag]        # default tag: index-latest
#
# Needs the `gh` CLI authenticated (`gh auth login`). An existing index/ is
# moved aside to index.bak.<timestamp> rather than clobbered.
set -euo pipefail

cd "$(dirname "$0")/.."
TAG="${1:-index-latest}"

if [ -d index ] && [ -f index/manifest.json ]; then
  BACKUP="index.bak.$(date +%s)"
  echo "existing index/ -> $BACKUP"
  mv index "$BACKUP"
fi

mkdir -p dist
echo "downloading index.tgz from release '$TAG'..."
gh release download "$TAG" --pattern index.tgz --dir dist --clobber
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
