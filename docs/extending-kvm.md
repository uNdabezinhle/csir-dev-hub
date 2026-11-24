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
4. **Golden images (image-publisher role).** Build them with Packer:
   - Windows 11 / Server 2025: sysprep, cloudbase-init, Coder agent bootstrap from `cloudbase-init.ps1.tftpl`, virtio drivers
   - Ubuntu cloud images

   Publish them to the pool as read-only base volumes. Workspaces use copy-on-write overlays (`base_volume_id`) plus a
   persistent data disk. Scan images with the `ImageScanner` extension (Trivy/Grype; for VMs use OpenSCAP) before
   publishing them in the portal catalog.
5. **Push `templates/windows-kvm`.** It is pinned to `dmacvicar/libvirt 0.8.3` and `terraform validate` passes. Set
   `libvirt_uri`, `base_image` and `network`, then run `coder templates push windows-kvm -d templates/windows-kvm` and
   set `published=true` for it in the portal catalog (admin → Images).
6. **Licensing.** Implement `LicenseBroker` (`stack/portal/app/extensions/base.py`) against the CSIR KMS/MAK seat pool.
   The portal already reserves seats per network profile (`windows-licensed.licence_seats`).
7. **RDP.** Run guacd on (or routed to) the KVM networks. The portal's `rdp_link()` takes the VM IP from the agent
   metadata. Use NLA and AD credentials. There is still no 3389 on the LAN (see docs/guacamole-rdp.md).
8. **KvmProvider extension.** Replace the stub with libvirt-python calls for portal-side power and snapshot actions
   (snapshot before a risky change, revert) and capacity reporting (free RAM/vCPU per host for admission control).

## Quick alternative for demos: `dockurr/windows`
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

## PoC box status
- `/dev/kvm` is present and passed through to containers (KVM API 12, `KVM_CREATE_VM` OK). A full guest boot was not
  demonstrated: the box has limited free RAM, and qemu test runs inside the sandbox were inconclusive.
- There's no libvirtd or golden image on the box, so `windows-kvm` is validated but not pushed.
