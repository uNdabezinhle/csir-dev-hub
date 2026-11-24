#!/usr/bin/env bash
# Build the Ubuntu golden image with Packer and publish it to the libvirt golden pool.
#   packer/ubuntu-golden/build.sh [--accel kvm|tcg] [--no-upgrade] [--no-publish]
# Runs as an unprivileged user (KVM needs membership of the kvm group; TCG needs nothing).
set -euo pipefail
cd "$(dirname "$0")"
ACCEL=kvm; UPGRADE=true; PUBLISH=1
while [ $# -gt 0 ]; do case $1 in --accel) ACCEL=$2; shift;; --no-upgrade) UPGRADE=false;; --no-publish) PUBLISH=0;; *) echo "bad arg $1"; exit 2;; esac; shift; done
PACKER=${PACKER:-$(command -v packer || echo /workspace/tools/packer)}
SEED=$(mktemp -d); trap 'rm -rf "$SEED"' EXIT
PW=$(openssl rand -hex 16)
HASH=$(openssl passwd -6 "$PW")
cat > "$SEED/user-data" <<UD
#cloud-config
ssh_pwauth: true
users:
  - name: packer
    passwd: "$HASH"
    lock_passwd: false
    sudo: "ALL=(ALL) ALL"
    shell: /bin/bash
UD
printf 'instance-id: packer-build\nlocal-hostname: golden\n' > "$SEED/meta-data"
OUT=$PWD/output-$(date +%Y%m%d-%H%M%S)
"$PACKER" init . >/dev/null
PKR_VAR_build_password="$PW" "$PACKER" build -color=false -var accelerator="$ACCEL" -var upgrade="$UPGRADE" \
  -var seed_dir="$SEED" -var output_dir="$OUT" ubuntu-golden.pkr.hcl
qemu-img info "$OUT/ubuntu-24.04-golden.qcow2" | grep -E 'virtual size|disk size'
if [ $PUBLISH = 1 ]; then ../../scripts/kvm-publish-image.sh --file "$OUT/ubuntu-24.04-golden.qcow2" ubuntu-24.04-golden; fi
