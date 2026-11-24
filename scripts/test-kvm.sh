#!/usr/bin/env bash
# KVM/libvirt path smoke test: boots a throw-away Ubuntu 24.04 VM from the golden pool through libvirt (copy-on-write
# overlay + cloud-init seed on the csir-dev network), waits for cloud-init to print a marker on the serial console,
# checks the DHCP lease, then destroys everything.  Usage: scripts/test-kvm.sh [timeout_s]
set -uo pipefail
cd "$(dirname "$0")/.."
export DOCKER_HOST="${DOCKER_HOST:-tcp://127.0.0.1:2375}"
LV=csir-devhub-libvirt-1; VM=csir-kvm-smoke; TMO=${1:-900}; pass=0; fail=0
ok(){ echo "PASS $*"; pass=$((pass+1)); }; ko(){ echo "FAIL $*"; fail=$((fail+1)); }
x(){ docker exec "$LV" bash -c "$*"; }
domtype=$(grep -qE '^KVM_ACCEL=kvm' .env && echo kvm || echo qemu)
x "virsh -q version" >/dev/null && ok "K1 libvirtd reachable in $LV ($(x 'virsh version' | grep -m1 hypervisor | xargs))" || ko "K1 libvirtd"
x "virsh -q vol-info --pool golden ubuntu-24.04-base.qcow2" >/dev/null && ok "K2 golden image golden/ubuntu-24.04-base.qcow2 present" || ko "K2 golden image (run scripts/kvm-publish-image.sh)"
x "virsh -q destroy $VM; virsh -q undefine $VM; virsh -q vol-delete --pool default $VM.qcow2; virsh -q vol-delete --pool default $VM-seed.iso" >/dev/null 2>&1
x "set -e; cd /tmp; mkdir -p seed && cd seed
cat > user-data <<'UD'
#cloud-config
hostname: csir-kvm-smoke
runcmd:
  - [ sh, -c, 'echo CSIR-KVM-SMOKE-OK \$(uname -r) \$(hostname -I) > /dev/ttyS0' ]
UD
printf 'instance-id: $VM\nlocal-hostname: $VM\n' > meta-data
genisoimage -quiet -output /var/lib/libvirt/images/$VM-seed.iso -volid cidata -joliet -rock user-data meta-data
virsh -q pool-refresh default
virsh -q vol-create-as default $VM.qcow2 8G --format qcow2 --backing-vol /var/lib/libvirt/images/golden/ubuntu-24.04-base.qcow2 --backing-vol-format qcow2
cat > /tmp/$VM.xml <<XML
<domain type='$domtype'>
  <name>$VM</name><memory unit='MiB'>1024</memory><vcpu>2</vcpu>
  <os><type arch='x86_64' machine='q35'>hvm</type><boot dev='hd'/></os>
  <cpu mode='custom' match='exact'><model fallback='allow'>qemu64</model></cpu>
  <devices>
    <disk type='file' device='disk'><driver name='qemu' type='qcow2'/><source file='/var/lib/libvirt/images/$VM.qcow2'/><target dev='vda' bus='virtio'/></disk>
    <disk type='file' device='cdrom'><driver name='qemu' type='raw'/><source file='/var/lib/libvirt/images/$VM-seed.iso'/><target dev='sda' bus='sata'/><readonly/></disk>
    <interface type='network'><source network='csir-dev'/><model type='virtio'/></interface>
    <serial type='file'><source path='/var/log/libvirt/qemu/$VM-serial.log'/><target port='0'/></serial>
    <console type='file'><source path='/var/log/libvirt/qemu/$VM-serial.log'/><target type='serial' port='0'/></console>
  </devices>
</domain>
XML
rm -f /var/log/libvirt/qemu/$VM-serial.log
virsh -q define /tmp/$VM.xml && virsh -q start $VM" && ok "K3 domain $VM defined + started (type=$domtype, CoW overlay on golden image, cloud-init seed)" || ko "K3 define/start"
t0=$(date +%s); marker=""
while [ $(( $(date +%s) - t0 )) -lt "$TMO" ]; do
  marker=$(x "grep -a -m1 'CSIR-KVM-SMOKE-OK' /var/log/libvirt/qemu/$VM-serial.log" 2>/dev/null | tr -d '\r') && [ -n "$marker" ] && break
  sleep 10
done
el=$(( $(date +%s) - t0 ))
[ -n "$marker" ] && ok "K4 guest booted + cloud-init ran in ${el}s: $marker" || { ko "K4 guest boot marker not seen in ${TMO}s"; x "tail -c 1500 /var/log/libvirt/qemu/$VM-serial.log"; }
lease=$(x "virsh -q net-dhcp-leases csir-dev" | grep -i "$VM" | awk '{print $5}')
[ -n "$lease" ] && ok "K5 DHCP lease on csir-dev: $lease" || ko "K5 no DHCP lease"
[ "${KEEP:-0}" = 1 ] || x "virsh -q destroy $VM; virsh -q undefine $VM; virsh -q vol-delete --pool default $VM.qcow2; virsh -q vol-delete --pool default $VM-seed.iso" >/dev/null 2>&1
echo "$pass passed, $fail failed"; [ $fail -eq 0 ]
