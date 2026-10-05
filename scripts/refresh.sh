#!/usr/bin/env bash
# Unattended refresh (run by the systemd timer): update checkouts, rebuild the
# index incrementally in a STAGING dir, swap it in atomically, and restart the
# service. Keeps main + release-tag checkouts and the answer corpus current with
# zero manual steps.
#
# Why staging + swap, not ingest-in-place: the live app holds the index (Chroma
# + SQLite) open. Writing to it concurrently can corrupt it, and the app won't
# see changes without reopening — so we build a copy, swap, and restart.
#
# Monitoring: this script never pings anything. Its exit status is the contract;
# the systemd unit reports it to healthchecks.io through scripts/hc_ping.sh
# (which also covers what a script cannot report about itself: timeouts, kills).
#
# Env knobs (all optional):
#   ENV_PY   path to the env python   (default: ~/miniforge3/envs/linc-bids-llm/bin/python)
#   SERVICE  systemd service to restart (default: bids-assistant)
#   SKIP_CHECKOUTS=1  skip the checkout update (faster; for testing)
#   SKIP_RESTART=1    don't restart the service (auto-skipped when systemctl absent)
#   SKIP_PUBLISH=1    don't re-publish the index release asset
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_PY="${ENV_PY:-$HOME/miniforge3/envs/linc-bids-llm/bin/python}"
SERVICE="${SERVICE:-bids-assistant}"
INDEX="index"
STAGING="index.staging"

log() { echo "[refresh $(date -u +%FT%TZ)] $*"; }

# No failure may be silent: say where the run died (the tail of this log is
# what the healthcheck alert e-mail shows).
trap 'log "FAILED: exit $? at line $LINENO"' ERR

# One KEY=value from .env. Prints nothing — and still succeeds — when the key or
# the file is missing (a bare `grep | cut` aborts the script under pipefail).
envval() {
  { grep -m1 -E "^[[:space:]]*$1[[:space:]]*=" .env 2>/dev/null || true; } \
    | cut -d= -f2- | tr -d '\r' \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^["'\'']//' -e 's/["'\'']$//'
}

# Publish auth for gh (the asset upload). Prefer a dedicated write-scoped token,
# GH_PUBLISH_TOKEN, else fall back to GITHUB_TOKEN. Harvesting (python) always
# uses GITHUB_TOKEN, where read-only is fine — so a read-only GITHUB_TOKEN for
# harvest + a write GH_PUBLISH_TOKEN for publish is the recommended split.
# An already-exported GH_TOKEN wins over both.
if [ -z "${GH_TOKEN:-}" ]; then
  tok="$(envval GH_PUBLISH_TOKEN)"
  if [ -z "$tok" ]; then tok="$(envval GITHUB_TOKEN)"; fi
  if [ -n "$tok" ]; then export GH_TOKEN="$tok"; fi
fi

if [ ! -f "$INDEX/manifest.json" ]; then
  echo "error: no live index at $INDEX/ — build it once with 'python -m src.ingest'." >&2
  exit 1
fi

# 1. checkouts: refresh main (fetch+reset) and clone any new release tags.
if [ "${SKIP_CHECKOUTS:-}" != "1" ]; then
  log "updating checkouts (main + new release tags)..."
  "$ENV_PY" -m src.checkouts
fi

# 2. seed a staging copy of the live index, then ingest incrementally into it.
log "seeding staging index from live..."
rm -rf "$STAGING"
cp -a "$INDEX" "$STAGING"

log "incremental ingest into staging..."
BIDS_INDEX_PATH="$STAGING" "$ENV_PY" -m src.ingest

# 3. validate the staging index before we trust it.
log "validating staging index..."
"$ENV_PY" - "$STAGING" <<'PY'
import json, sys, pathlib
staging = pathlib.Path(sys.argv[1])
m = json.loads((staging / "manifest.json").read_text())
n = sum(m.get("chunks", {}).values())
assert n > 0, "staging index is empty"
print(f"  staging OK: {n} chunks, built {m.get('built_at')}")
PY

# 4. atomic swap: move the fresh index into place, drop the old one.
log "swapping index in..."
PREV="${INDEX}.prev.$$"
mv "$INDEX" "$PREV"
mv "$STAGING" "$INDEX"
rm -rf "$PREV"

# 5. restart so the app reopens the new index (its handles point at the old one).
if [ "${SKIP_RESTART:-}" != "1" ] && command -v systemctl >/dev/null 2>&1; then
  log "restarting $SERVICE..."
  sudo systemctl restart "$SERVICE"
else
  log "skipping service restart (SKIP_RESTART set or systemctl absent)."
fi

# 6. re-publish the release asset (backup + up-to-date index for local dev).
#    Non-fatal: the live index is already swapped in; a publish failure (no gh,
#    unauthenticated, or a read-only token) only means the downloadable copy lags.
if [ "${SKIP_PUBLISH:-}" != "1" ]; then
  log "publishing index asset..."
  if scripts/package_index.sh --upload; then
    log "asset published."
  else
    log "WARNING: asset publish failed (gh missing / unauthenticated / read-only"
    log "         token). The live index is current regardless; skipping."
  fi
fi

log "done."
