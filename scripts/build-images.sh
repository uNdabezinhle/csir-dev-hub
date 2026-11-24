#!/usr/bin/env bash
# Build the workspace images used by the Coder templates. Usage: scripts/build-images.sh [server|desktop|kali|all]
set -euo pipefail
cd "$(dirname "$0")/.."
export DOCKER_HOST="${DOCKER_HOST:-tcp://127.0.0.1:2375}"
what="${1:-all}"
b() { echo "==> docker build $*"; docker build -q "$@" >/dev/null; }
if [[ $what == all || $what == server ]]; then
  for v in 24.04 26.04; do b --build-arg UBUNTU_VERSION=$v -t csir-devhub/ubuntu-server:$v images/ubuntu-server; done
fi
if [[ $what == all || $what == desktop ]]; then
  b --build-arg UBUNTU_VERSION=24.04 -t csir-devhub/ubuntu-desktop:24.04 images/ubuntu-desktop
fi
if [[ $what == all || $what == kali ]]; then
  b -t csir-devhub/kali:rolling images/kali
fi
docker images --format '{{.Repository}}:{{.Tag}}  {{.Size}}' | grep '^csir-devhub/' | sort
