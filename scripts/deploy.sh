#!/usr/bin/env bash
# Pull the latest main and restart the service. Run on the server.
#
#   scripts/deploy.sh            # git pull + deps + restart
#   REFRESH_INDEX=1 scripts/deploy.sh   # also re-fetch the prebuilt index
#
# ENV_PY defaults to the miniforge env python; override if yours differs.
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_PY="${ENV_PY:-$HOME/miniforge3/envs/linc-bids-llm/bin/python}"
SERVICE="${SERVICE:-bids-assistant}"

echo "pulling main..."
git pull --ff-only

echo "syncing deps..."
"$ENV_PY" -m pip install -q -r requirements.txt

if [ "${REFRESH_INDEX:-}" = "1" ]; then
  echo "refreshing index..."
  scripts/fetch_index.sh
fi

echo "restarting $SERVICE..."
sudo systemctl restart "$SERVICE"
sleep 2
sudo systemctl --no-pager --lines=0 status "$SERVICE" || true
echo "deployed $(git rev-parse --short HEAD)"
