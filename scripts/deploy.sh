#!/usr/bin/env bash
# Pull the latest main and restart the service. Run on the server.
#
#   scripts/deploy.sh            # git pull + deps + restart
#   REFRESH_INDEX=1 scripts/deploy.sh   # also re-fetch the prebuilt index
#
# ENV_PY defaults to the miniforge env python; override if yours differs.
#
# Never runs alongside the nightly refresh: both take .refresh.lock (see
# refresh.sh). If a refresh holds it, this exits 75 at once without touching
# anything — wait for the refresh to finish, then retry. Without flock (macOS)
# it runs unlocked.
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_PY="${ENV_PY:-$HOME/miniforge3/envs/linc-bids-llm/bin/python}"
SERVICE="${SERVICE:-bids-assistant}"

if command -v flock >/dev/null 2>&1; then
  exec 9>.refresh.lock
  if ! flock -n 9; then
    echo "a refresh is running — wait for it to finish, then retry" >&2
    echo "(journalctl -fu bids-assistant-refresh.service follows it)" >&2
    exit 75
  fi
fi

echo "pulling main..."
git pull --ff-only

echo "syncing deps..."
"$ENV_PY" -m pip install -q -r requirements.txt

if [ "${REFRESH_INDEX:-}" = "1" ]; then
  echo "refreshing index..."
  # the lock is ours already; fetch_index.sh must not try to take it again
  REFRESH_LOCK_HELD=1 scripts/fetch_index.sh
fi

echo "restarting $SERVICE..."
sudo systemctl restart "$SERVICE"
sleep 2
sudo systemctl --no-pager --lines=0 status "$SERVICE" || true
echo "deployed $(git rev-parse --short HEAD)"
