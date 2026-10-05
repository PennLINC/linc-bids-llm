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
#   0     every step worked
#   3     the new index is live, but the app restart and/or the asset publish
#         failed (SKIP_PUBLISH=1 turns publishing off)
#   else  the run died earlier — normally before the swap, live index untouched
#
# One run at a time: refresh.sh, deploy.sh and fetch_index.sh all take the same
# lock (.refresh.lock at the repo root, via flock) because two of them at once
# wreck index/ — a second refresh deletes the first one's staging dir and the
# first then fails its swap with no live index left; a deploy pip-installs
# under a running ingest. systemd only de-duplicates its own starts, not a
# hand-run script. Without flock (macOS) the scripts run unlocked.
#
# Env knobs (all optional):
#   ENV_PY   path to the env python   (default: ~/miniforge3/envs/linc-bids-llm/bin/python)
#   SERVICE  systemd service to restart (default: bids-assistant)
#   LOCK_WAIT  seconds to wait for a running refresh/deploy before giving up
#              with exit 75 (default: 600)
#   SKIP_CHECKOUTS=1  skip the checkout update (faster; for testing)
#   SKIP_RESTART=1    don't restart the service (auto-skipped when systemctl absent)
#   SKIP_PUBLISH=1    don't re-publish the index release asset
#   ALLOW_SHRINK=1    swap in a staging index even if a source lost >20% of its
#                     chunks (validation refuses that by default: a harvest that
#                     failed quietly looks like a corpus that shrank)
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

# Take the lock first, before even looking at index/ (a concurrent run may be
# mid-swap). The lock lives on fd 9 for the rest of the run; a leftover
# .refresh.lock file is harmless (flock locks the open file, not its presence).
if command -v flock >/dev/null 2>&1; then
  exec 9>.refresh.lock
  if ! flock -n 9; then
    log "another refresh/deploy is running (holds .refresh.lock); waiting up to ${LOCK_WAIT:-600}s..."
    if ! flock -w "${LOCK_WAIT:-600}" 9; then
      log "still locked after ${LOCK_WAIT:-600}s; giving up without touching anything (exit 75)."
      exit 75
    fi
    log "lock acquired."
  fi
fi

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

# 3. validate the staging index before we trust it. Besides "not empty", compare
#    per-source chunk counts with the live manifest: a source that lost a large
#    share of its chunks is almost always a harvest that failed quietly (a
#    rate-limited or 404'd NeuroStars listing reads as "no topics" and every
#    topic not seen gets pruned), not a corpus that shrank. Docs + issues keep
#    the total well above zero, so the total alone never catches it.
log "validating staging index..."
"$ENV_PY" - "$STAGING" "$INDEX" <<'PY'
import json, os, sys, pathlib
staging, live = (pathlib.Path(p) for p in sys.argv[1:3])
m = json.loads((staging / "manifest.json").read_text())
new = m.get("chunks", {})
n = sum(new.values())
assert n > 0, "staging index is empty"

MIN_CHUNKS, MAX_LOSS = 50, 0.20   # sources under MIN_CHUNKS may shrink freely
old = json.loads((live / "manifest.json").read_text()).get("chunks", {})
shrunk = [(src, old[src], new.get(src, 0)) for src in sorted(old)
          if old[src] >= MIN_CHUNKS and new.get(src, 0) < old[src] * (1 - MAX_LOSS)]
for src, was, now in shrunk:
    print(f"  {src}: {was} -> {now} chunks ({(was - now) * 100 // was}% lost)",
          file=sys.stderr)
if shrunk and os.environ.get("ALLOW_SHRINK") != "1":
    names = ", ".join(f"{src} {was} -> {now}" for src, was, now in shrunk)
    sys.exit(f"error: staging index shrank: {names} chunks (limit {MAX_LOSS:.0%} of a "
             f"source with {MIN_CHUNKS}+ chunks). Looks like a failed harvest; the "
             "live index is kept. Re-run with ALLOW_SHRINK=1 if the shrink is genuine.")
if shrunk:
    print("  WARNING: ALLOW_SHRINK=1 set; swapping in the shrunken index anyway.")
print(f"  staging OK: {n} chunks, built {m.get('built_at')}")
PY

# 4. atomic swap: move the fresh index into place, drop the old one.
log "swapping index in..."
PREV="${INDEX}.prev.$$"
mv "$INDEX" "$PREV"
mv "$STAGING" "$INDEX"
rm -rf "$PREV"

# From here on the new index is live, so a failing step no longer stops the
# run: finish the other steps, then exit 3 so the failure is still reported.
problems=""

# 5. build the release tarball NOW, while nothing has the new index open. Once
#    restarted, the app rewrites chroma.sqlite3 as soon as a session connects,
#    and GNU tar counts a file that changes under it as a failure.
tarball=""
if [ "${SKIP_PUBLISH:-}" != "1" ]; then
  log "packaging index tarball..."
  if scripts/package_index.sh; then
    tarball=1
  else
    log "ERROR: packaging the index tarball failed; nothing to publish."
    problems="$problems package"
  fi
fi

# 6. restart so the app reopens the new index (its handles point at the old one).
if [ "${SKIP_RESTART:-}" != "1" ] && command -v systemctl >/dev/null 2>&1; then
  log "restarting $SERVICE..."
  if ! sudo systemctl restart "$SERVICE"; then
    log "ERROR: restarting $SERVICE failed — the app still serves the old index, or"
    log "       is down. Fix by hand: sudo systemctl restart $SERVICE"
    problems="$problems restart"
  fi
else
  log "skipping service restart (SKIP_RESTART set or systemctl absent)."
fi

# 7. re-publish the release asset (backup + up-to-date index for local dev).
#    It cannot undo the refresh — the live index is already swapped in — but a
#    failure is reported (exit 3): an expired GH_PUBLISH_TOKEN would otherwise
#    leave the downloadable copy stale with nobody told. Opt out: SKIP_PUBLISH=1.
if [ -n "$tarball" ]; then
  log "publishing index asset..."
  if scripts/package_index.sh --upload-only; then
    log "asset published."
  else
    log "ERROR: asset publish failed (gh missing / unauthenticated / read-only or"
    log "       expired token / network). The live index is current regardless."
    problems="$problems publish"
  fi
fi

if [ -n "$problems" ]; then
  log "FAILED after the swap:$problems. The new index is in place; see ERROR above."
  exit 3
fi
log "done."
