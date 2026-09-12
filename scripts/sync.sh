#!/usr/bin/env bash
# Push the orchestrator to the server and restart it.
#   ./scripts/sync.sh claude@vps.tailnet.ts.net
set -euo pipefail
TARGET="${1:?usage: sync.sh user@host}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

rsync -az --delete \
  --exclude '__pycache__' --exclude '.venv' --exclude 'data' --exclude '*.pyc' \
  "$HERE/orchestrator/" "$TARGET:/opt/ccremote/orchestrator/"

ssh "$TARGET" 'sudo systemctl restart ccremote && sleep 2 && systemctl is-active ccremote'
echo "synced and restarted"
