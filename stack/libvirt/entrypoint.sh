#!/bin/bash
# Starts virtlogd + libvirtd (TCP 16509, reachable only on the private Docker network), defines the storage pool and
# the three CSIR networks (dev / isolated / windows-licensed).
#   KVM_ACCEL=tcg (default on the PoC box): /dev/kvm is hidden from libvirt so no guest can use KVM. On the PoC box the
#   L0 hypervisor's nested VMX is broken: creating a KVM vCPU triggers "kernel BUG at arch/x86/kvm/x86.c:689"
#   (kvm_spurious_fault in vmx_vcpu_create). Guests run under QEMU TCG (software emulation) instead.
#   KVM_ACCEL=kvm: keep /dev/kvm (real KVM hosts).
set -e
if [ "${KVM_ACCEL:-tcg}" != "kvm" ] && [ -e /dev/kvm ]; then rm -f /dev/kvm; echo "[libvirt] KVM_ACCEL=${KVM_ACCEL:-tcg}: /dev/kvm hidden"; fi
mkdir -p /var/lib/libvirt/images /var/lib/libvirt/images/golden /run/libvirt /var/log/libvirt/qemu
virtlogd -d
libvirtd -d -l
for i in $(seq 1 30); do virsh -c qemu:///system version >/dev/null 2>&1 && break; sleep 1; done
virsh pool-info default >/dev/null 2>&1 || { virsh pool-define-as default dir --target /var/lib/libvirt/images; virsh pool-autostart default; }
virsh pool-info default | grep -q running || virsh pool-start default
# Read-only golden images published by the image pipeline (scripts/kvm-publish-image.sh, packer/).
virsh pool-info golden >/dev/null 2>&1 || { virsh pool-define-as golden dir --target /var/lib/libvirt/images/golden; virsh pool-autostart golden; }
virsh pool-info golden | grep -q running || virsh pool-start golden
for f in /etc/csir-libvirt/networks/*.xml; do
  n=$(basename "$f" .xml)
  virsh net-info "$n" >/dev/null 2>&1 || { virsh net-define "$f"; virsh net-autostart "$n"; }
  virsh net-info "$n" | grep -qE 'Active:\s+yes' || virsh net-start "$n" || echo "[libvirt] WARN network $n failed to start"
done
# Re-start domains that were running (autostart) after a container restart.
virsh list --all --autostart --name | while read -r d; do [ -n "$d" ] && virsh start "$d" 2>/dev/null || true; done
echo "[libvirt] ready: $(virsh version | grep -E 'library|hypervisor' | tr '\n' ' ')"
exec tail -F /var/log/libvirt/libvirtd.log /dev/null 2>/dev/null
