#!/usr/bin/env bash
# Build the Windows Server 2022 evaluation golden image and publish it to the libvirt golden pool.
#   packer/windows-2022/build.sh [--iso-checksum sha256:<hash>] [--no-publish]
# REQUIRES a host with working KVM (kvm group membership). Under TCG an install takes many hours - not supported.
set -euo pipefail
cd "$(dirname "$0")"
SUM=none; PUBLISH=1
while [ $# -gt 0 ]; do case $1 in --iso-checksum) SUM=$2; shift;; --no-publish) PUBLISH=0;; *) echo "bad arg $1"; exit 2;; esac; shift; done
[ -w /dev/kvm ] || { echo "ERROR: /dev/kvm not usable by $(id -un) - build Windows golden images on a KVM host"; exit 3; }
PACKER=${PACKER:-$(command -v packer || echo /workspace/tools/packer)}
[ -f virtio-win.iso ] || curl -fL -o virtio-win.iso https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso
ANS=$(mktemp -d); trap 'rm -rf "$ANS"' EXIT
PW="Csir-$(openssl rand -hex 12)!"
sed "s|__ADMIN_PASSWORD__|$PW|g" answer/autounattend.xml.tpl > "$ANS/autounattend.xml"
OUT=$PWD/output-$(date +%Y%m%d-%H%M%S)
"$PACKER" init . >/dev/null
PKR_VAR_admin_password="$PW" "$PACKER" build -color=false -var iso_checksum="$SUM" \
  -var answer_dir="$ANS" -var output_dir="$OUT" windows-2022.pkr.hcl
[ $PUBLISH = 1 ] && ../../scripts/kvm-publish-image.sh --file "$OUT/win2022-eval-golden.qcow2" win2022-eval-golden
