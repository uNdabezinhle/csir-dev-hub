#!/usr/bin/env bash
# Publish a Linux golden image into the libvirt "golden" pool (image-publisher pipeline, Linux path).
#   scripts/kvm-publish-image.sh ubuntu-24.04            # official Ubuntu minimal cloud image, SHA256-verified
#   scripts/kvm-publish-image.sh --file out/x.qcow2 NAME # publish a Packer build output (packer/ubuntu-golden)
# VM workspaces use copy-on-write overlays on top of these volumes (base_volume_name in templates/ubuntu-vm).
set -euo pipefail
cd "$(dirname "$0")/.."
export DOCKER_HOST="${DOCKER_HOST:-tcp://127.0.0.1:2375}"
LV=csir-devhub-libvirt-1
if [ "${1:-}" = "--file" ]; then
  src="$2"; name="$3"
  docker cp "$src" "$LV:/var/lib/libvirt/images/golden/$name.qcow2"
else
  rel="${1:-ubuntu-24.04}"
  case "$rel" in
    # Standard server cloud images (generic kernel). The *minimal* images ship the linux-kvm kernel, which has no
    # SATA/IDE CD-ROM driver, so the cloud-init seed ISO that libvirt attaches as a cdrom is never found.
    ubuntu-24.04) base=https://cloud-images.ubuntu.com/releases/noble/release; file=ubuntu-24.04-server-cloudimg-amd64.img ;;
    ubuntu-26.04) base=https://cloud-images.ubuntu.com/releases/resolute/release; file=ubuntu-26.04-server-cloudimg-amd64.img ;;
    *) echo "unknown release $rel"; exit 2 ;;
  esac
  name="$rel-base"
  docker exec "$LV" bash -ec "cd /var/lib/libvirt/images/golden
    curl -fsSL -o SHA256SUMS.$rel $base/SHA256SUMS
    [ -f $file ] || curl -fsSL -o $file $base/$file
    grep ' \*\?$file\$' SHA256SUMS.$rel | sed 's/\*//' | sha256sum -c -
    qemu-img convert -O qcow2 $file $name.qcow2 && rm -f $file"
fi
docker exec "$LV" bash -ec "chmod 0444 /var/lib/libvirt/images/golden/$name.qcow2; virsh -q pool-refresh golden; virsh vol-info --pool golden $name.qcow2"
echo "published golden image: golden/$name.qcow2"
