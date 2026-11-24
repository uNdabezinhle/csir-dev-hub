# Extending the PoC to full Coder + KVM (on-prem)

The PoC runs everything on one box: Coder with the Docker provisioner, plus a Docker sidecar daemon. The annex target is a
**hybrid**: containers for Linux engineering workspaces, and **KVM/libvirt VMs** for Windows, kernel/driver work and
full-OS desktops. Both are managed by the same Coder control plane and portal.

## Target topology

```
             +-------------------- control plane (VM or 3-node k8s) --------------------+
 users --TLS--> Traefik/NGINX (wildcard *.devhub.csir.co.za) --> Coder (2+ replicas, HA)  |
             |   Keycloak (HA) <- LDAP/AD     Portal     Guacamole     Prometheus/Grafana   |
             |   PostgreSQL (Patroni / managed)                        Loki (audit logs)    |
             +---------------------------------------------------------------------------+
                         | provisioner daemons (coder provisionerd, tagged)
      +------------------+-------------------+-------------------------------+
      | container hosts (Docker or k8s)      | KVM hosts (libvirtd + qemu)    |
      | tag: type=docker                     | tag: type=kvm                  |
      | ubuntu-server / desktop / kali       | windows-kvm, ubuntu-vm         |
      +--------------------------------------+--------------------------------+
```

## Steps

1. **Externalise the control plane.** Move PostgreSQL to an HA cluster. Run Coder with `CODER_ACCESS_URL=https://devhub.csir...`
   and `CODER_WILDCARD_ACCESS_URL=*.devhub.csir...`: subdomain apps isolate IDE origins and let site owners and
   auditors open apps where policy allows. Put TLS on every endpoint (Keycloak needs `KC_HOSTNAME=https://sso...`).
2. **External provisioners.** On each worker, run `coder provisioner start --tag type=docker` (or `type=kvm`) with a
   scoped provisioner key. Add `coder_workspace_tags { tags = { type = "kvm" } }` to templates so jobs route to the right host.
3. **KVM hosts.** Ubuntu Server 24.04 with `qemu-kvm libvirt-daemon-system ovmf swtpm`. Check with `kvm-ok`. Create
   storage pool `csir-pool` (LVM thin or ZFS) and networks:
   - `csir-dev`: NAT through the egress proxy
   - `csir-isolated`: no forward
   - `csir-windows`: routed to KMS / AD / WSUS only

   The provisioner reaches libvirt through `qemu+ssh://provisioner@kvmhost/system`, or locally on the host.
4. **Golden images (image-publisher role).** Build them with Packer (`packer/`):
   - `packer/windows-2022`: Microsoft evaluation ISO or VLSC media, virtio drivers, cloudbase-init, Windows Update, sysprep
   - `packer/ubuntu-golden`: cloud image, updates, qemu-guest-agent, cleanup

   Each `build.sh` publishes its image to the `golden` pool as a read-only base volume (`scripts/kvm-publish-image.sh --file`).
   Workspaces use copy-on-write overlays plus a persistent data disk. Scan the images (Trivy for container images; for
   VM images, `trivy vm`/OpenSCAP) and approve them in the portal before the catalog item is published.
5. **Push the VM templates.** `templates/ubuntu-vm` and `templates/windows-kvm` (pinned to `dmacvicar/libvirt 0.8.3`)
   are both pushed on the PoC. On-prem, set `libvirt_uri` (`qemu+ssh://...`), `domain_type = "kvm"` and the golden image,
   push, then publish the catalog item (admin → Images).
6. **Licensing.** The portal's `LicenseBroker` (`stack/portal/app/extensions/licence.py`) manages seat pools. Point the
   `windows-kms` pool at the real CSIR KMS host (`kms_host`) and size it in Admin → Licences (docs/licensing-images-billing.md).
7. **RDP.** The portal's `KvmProvider.rdp_forward()` sets up a DNAT on the libvirt host that accepts connections only
   from guacd, and Guacamole connects with the per-workspace credential (NLA). On-prem, either route guacd to the KVM
   networks directly or keep the DNAT. There is still no 3389 on the LAN (see docs/guacamole-rdp.md).
8. **KvmProvider.** Implemented with `virsh` in the libvirt container: power, snapshots (create, revert, delete), capacity,
   domains, pools and networks. For multi-host, switch the transport to `qemu+ssh` per host and use capacity for admission control.

## Quick alternative for demos: `dockurr/windows` (not used; needs working KVM)
Windows inside a container (QEMU+KVM in Docker), for labs without libvirt:
```hcl
resource "docker_container" "win" {
  image   = "dockurr/windows:latest"
  devices { host_path = "/dev/kvm" }
  capabilities { add = ["NET_ADMIN"] }
  env     = ["VERSION=11", "RAM_SIZE=8G", "CPU_CORES=4", "DISK_SIZE=64G"]
  networks_advanced { name = docker_network.ws.name }  # private: guacd reaches :3389, nothing published
  volumes { volume_name = "win-${local.ws_slug}" container_path = "/storage" }
}
```
Caveats: the first boot downloads an ISO and installs it (about 20–40 minutes), there's no Coder agent inside Windows
unless you add an OEM script, and licensing is still the customer's responsibility. Good for a demo; use libvirt for production.

## Phase 2 on the PoC box: what is verified, what is blocked

### What runs here
| Piece | Where | Status |
|---|---|---|
| libvirt 9.0 + QEMU 7.2 | `stack/libvirt` container (`docker-compose.kvm.yml`, enabled by `KVM_ENABLED=true`) | **Verified.** libvirtd on TCP 16509 is reachable only on the private `csir-devhub-core` network (never published). Pools `default` and `golden`; networks `csir-dev` (NAT), `csir-isolated` (no forward) and `csir-windows-licensed` (NAT, which stands in for a KMS-only routed network). Domain and network definitions persist in the `libvirt-qemu-etc` volume. |
| Linux VM boot through libvirt | `scripts/test-kvm.sh` | **Verified, 5/5.** It creates a CoW overlay on the golden image plus a cloud-init seed, boots, cloud-init runs, and the VM gets a DHCP lease on `csir-dev`. Boot to cloud-init takes about 90 s under TCG. |
| Linux golden-image pipeline | `packer/ubuntu-golden/build.sh` | **Verified.** It takes the official SHA256-verified cloud image, applies security updates, adds qemu-guest-agent and base tools, resets machine-id, SSH host keys and cloud-init, then compresses to qcow2 and publishes it as `golden/ubuntu-24.04-golden.qcow2`. Build time is 10 min 26 s under TCG. |
| VM workspace in Coder | `templates/ubuntu-vm` (dmacvicar/libvirt 0.8.3, `qemu+tcp://libvirt/system`) | **Verified.** `admin/demo-vm` runs with the Coder agent connected. `coder ssh` works (kernel 6.8, `systemd-detect-virt` reports qemu). Web terminal, VS Code Desktop and code-server are available, egress works on the dev profile, and stop/start keep the disk. |
| Portal KVM provider | `stack/portal/app/extensions/kvm.py`, Admin → KVM, `/api/kvm` | **Verified.** It reports capacity (accel, CPUs, RAM, pools, networks), lists domains, and handles power actions and snapshots (create, revert, delete). It calls `virsh` in the libvirt container through the Docker API. |
| Brokered RDP to VMs | portal `rdp_forward()`, which sets a DNAT on the libvirt host that only guacd can reach | **Verified** with xrdp in demo-vm (`scripts/test-rdp.py demo-vm`). No port 3389 is published anywhere. |
| `windows-kvm` template | `templates/windows-kvm` | **Pushed to Coder** but not buildable here (see the blocker below). It is unpublished in the portal catalog. It uses q35 + OVMF Secure Boot + swtpm TPM 2.0 (with an XSLT that moves the seed cdrom to SATA and enables SMM), a CoW system disk, a persistent D: disk, a cloudbase-init user-data that starts the Coder agent and creates a per-workspace RDP user (random password kept only in Terraform state, which the portal hands to Guacamole), and the `csir-windows-licensed` network. |
| Windows golden-image pipeline | `packer/windows-2022` | **Validated** with `packer validate`, but **not built**. It uses the official Microsoft Evaluation Center ISO (Windows Server 2022, the `go.microsoft.com/fwlink/p/?LinkID=2195280` link, which resolves to `SERVER_EVAL_x64FRE_en-us.iso`, 5.0 GB) plus virtio-win drivers, an autounattend file with a build-only password generated per build, WinRM for the build only, virtio guest tools, RDP+NLA, cloudbase-init, Windows Update, cleanup and sysprep. `build.sh` refuses to run without a usable `/dev/kvm`. |

### Blocker: nested KVM is broken on this box
`/dev/kvm` exists and `KVM_CREATE_VM` succeeds, but creating the first **vCPU** crashes the kernel's KVM module:
```
kernel BUG at arch/x86/kvm/x86.c:689!
Oops: invalid opcode: 0000 [#6] PREEMPT SMP NOPTI
CPU: 6 UID: 0 PID: 505785 Comm: qemu-system-x86 Tainted: G      D            6.12.113+ #1
RIP: 0010:kvm_spurious_fault+0xe/0x10
Call Trace: vmx_vcpu_create+0xac/0x490  kvm_arch_vcpu_create+0x1e9/0x300  kvm_vm_ioctl_create_vcpu+0x185/0x4f0
```
The guest never executes an instruction (0 bytes on the serial console), while the same image boots under TCG. The
outer (L0) hypervisor of the PoC VM does not support VMX operations for nested guests. Each attempt also taints
the kernel, so:
- the libvirt container **hides `/dev/kvm`** (`KVM_ACCEL=tcg`, the default in `.env`). Only `type=qemu` (TCG) domains can be created, and a `type=kvm` domain fails cleanly at define time instead of crashing the box.
- Packer runs as an unprivileged user that has no access to `/dev/kvm`.

Consequences:
- **Linux VMs:** they work under TCG. They are slower (about 90 s to boot, CPU-heavy work is about 5–10× slower) and use about 1 GB extra RAM per guest for the translation cache. `LIBVIRT_MEM_LIMIT=5g` allows one 2 GB guest plus headroom. A 2 GB guest was OOM-killed under the earlier 3 GB limit.
- **Windows:** not feasible here. A Windows Server 2022 install plus updates under TCG would take many hours of 4 vCPUs at 100 % and needs 4 GB or more of guest RAM, which would starve the running stack. No Windows image was downloaded or installed. The Windows workspace path is complete up to "build the golden image on a KVM host".

### To finish Windows on a real KVM host
1. On the KVM host (Ubuntu 24.04/26.04): install `qemu-kvm libvirt-daemon-system ovmf swtpm`. Check with `kvm-ok` and confirm `/dev/kvm` is writable by the build user (`kvm` group).
2. `packer/windows-2022/build.sh --iso-checksum sha256:<hash of SERVER_EVAL_x64FRE_en-us.iso>` builds and publishes `golden/win2022-eval-golden.qcow2`. It takes about 60–90 min with KVM, longer with Windows Update.
3. Point the template at that host. In the PoC compose that means setting `KVM_ACCEL=kvm` (the container keeps `/dev/kvm`). On-prem, set `libvirt_uri = "qemu+ssh://provisioner@kvm-host/system"` and add a `coder_workspace_tags` `type=kvm` provisioner.
4. `coder templates push windows-kvm -d templates/windows-kvm`. In the portal, scan and approve it (Admin → Images) and publish the catalog item.
5. Create a workspace on the `windows-licensed` profile. The licence broker checks out a seat. **Desktop (RDP)** in the portal sets up the libvirt DNAT (reachable only from guacd) and opens Guacamole with the per-workspace credential (NLA).
6. For a licensed build, swap the evaluation ISO for CSIR VLSC media and keep KMS activation through `kms.csir.local:1688` (pool `windows-kms`).
