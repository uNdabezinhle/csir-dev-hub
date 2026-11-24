# ubuntu-vm template

This template runs an Ubuntu 24.04 **virtual machine** workspace on libvirt/QEMU (`dmacvicar/libvirt` 0.8.3). It is the
verified proof of the KVM workspace path, and Windows (`templates/windows-kvm`) uses the same mechanics.

- **Disk:** the system disk is a CoW overlay (`backing_store`) of a golden image in the `golden` pool.
  - The default is `ubuntu-24.04-golden.qcow2`, built by `packer/ubuntu-golden`.
  - `ubuntu-24.04-base.qcow2` is the plain cloud image from `scripts/kvm-publish-image.sh ubuntu-24.04`.
  - Existing disks keep their base image when the template default changes (`ignore_changes`).
- **cloud-init seed:** a NoCloud ISO with a fresh name for each agent token. It creates the `coder` user and installs a systemd unit that runs the Coder agent (`/opt/coder/init.sh`). On every rebuild the unit is restarted with the new token.
- **Parameters:** CPU 1–4, memory 1–8 GB, disk size, network profile (`dev` = `csir-dev` NAT, `isolated` = `csir-isolated` with no egress), and code-server on/off.
- **Machine:** `pc` (i440fx), because the provider attaches the seed cdrom on IDE and q35 has no IDE. `autostart=true`, so VMs come back after a libvirt restart.
- **Access:** web terminal, `coder ssh`, VS Code Desktop, and code-server (optional).
- **RDP:** cloud-init sets a random password for the `coder` user, generated per workspace (`random_password.rdp`). It is kept in Terraform state and in a sensitive metadata item. SSH password authentication stays off. Once xrdp is installed in the guest (`sudo apt-get install -y xrdp`), the portal's **Desktop (RDP)** brokers it through Guacamole with that credential. The connection uses a DNAT on the libvirt host that is reachable only from guacd. Verified with `scripts/test-rdp.py demo-vm`: 5/5 checks pass, and xrdp-sesman logs `created session (access granted): username coder` from guacd's address.

On the PoC box, `domain_type = "qemu"` (TCG) is used because nested KVM crashes the host kernel (see docs/extending-kvm.md).
On a KVM host, set `domain_type = "kvm"`.

```
coder templates push ubuntu-vm -d templates/ubuntu-vm
coder create <user>/vm1 -t ubuntu-vm --use-parameter-defaults
```
