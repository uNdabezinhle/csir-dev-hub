#!/usr/bin/env bash
# Stop the platform stack. Workspaces (Coder-managed containers) are stopped via Coder, not removed.
# Usage: scripts/stop.sh [--workspaces]   (--workspaces also stops all running Coder workspaces first)
set -euo pipefail
cd "$(dirname "$0")/.."
export DOCKER_HOST="${DOCKER_HOST:-tcp://127.0.0.1:2375}"
if [ "${1:-}" = "--workspaces" ] && command -v coder >/dev/null; then
  coder list --all -o json 2>/dev/null | python3 -c 'import json,sys
for w in json.load(sys.stdin):
    if w["latest_build"]["status"]=="running": print(w["owner_name"]+"/"+w["name"])' | while read -r ws; do
    echo "stopping $ws"; coder stop "$ws" --yes >/dev/null
  done
fi
./scripts/dc.sh stop
echo "stopped (volumes kept). Remove everything incl. data: ./scripts/dc.sh down -v  (destructive)"
