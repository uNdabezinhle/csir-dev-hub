#!/bin/bash
# Golden image baseline: security updates, guest agent, tooling the Coder agent/code-server need.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
cloud-init status --wait || true
apt-get update
[ "${UPGRADE:-true}" = true ] && apt-get -y -o Dpkg::Options::=--force-confold upgrade
apt-get install -y --no-install-recommends qemu-guest-agent curl ca-certificates git jq unzip htop
systemctl enable qemu-guest-agent || true
echo "CSIR Dev Hub golden image $(date -u +%Y-%m-%dT%H:%MZ)" > /etc/csir-golden-release
