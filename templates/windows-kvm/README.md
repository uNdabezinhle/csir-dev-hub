# windows-kvm template

This template runs Windows Server 2022 (or Windows 11 / Server 2025) as a **KVM/QEMU VM through libvirt** (`dmacvicar/libvirt` 0.8.3).

- **Agent:** the Coder agent runs inside the guest as a scheduled task, started by cloudbase-init user data (`cloudbase-init.ps1.tftpl`).
- **Firmware and machine:** q35, OVMF Secure Boot with Microsoft keys (`OVMF_VARS_4M.ms.fd`), and an swtpm TPM 2.0. `cdrom-sata.xsl` moves the provider's IDE seed cdrom to SATA and enables SMM.
- **Disks:** a CoW overlay of a sysprepped golden image (`golden/win2022-eval-golden.qcow2`, built by `packer/windows-2022`) and a separate persistent data disk, which appears as D:.
- **Network:** the `csir-windows-licensed` libvirt network. The licence broker checks out a seat from pool `windows-kms` (or `windows-eval`).
- **RDP:** it is never published. A local user `coder` gets a random password per workspace (`random_password.rdp`), kept only in Terraform state. The portal reads it from the workspace's resource metadata (marked sensitive) and puts it in Guacamole's signed JSON token. NLA is on, and the guest firewall allows RDP only from its local subnet. The portal adds a DNAT on the libvirt host that accepts connections only from guacd. As a fallback, use `coder port-forward <ws> --tcp 3389`.

## Status on the PoC box
- Pushed to Coder (`coder templates push windows-kvm`). `terraform validate` passes. The portal catalog item is **unpublished**.
- **Not built.** The PoC box's nested KVM crashes the host kernel when it creates a vCPU (`kernel BUG at arch/x86/kvm/x86.c:689`, in `vmx_vcpu_create`). The libvirt container therefore runs TCG only, which is impractical for Windows. Full details and the steps for a real KVM host are in [docs/extending-kvm.md](../../docs/extending-kvm.md).
- The same libvirt, cloud-init, agent and brokered-RDP path is verified end-to-end with `templates/ubuntu-vm` (workspace `admin/demo-vm`).

## Media and licensing
Use only legitimate media: either the Microsoft Evaluation Center ISO (180-day evaluation, pool `windows-eval`) or
CSIR volume-licensed media with KMS activation (pool `windows-kms`). Never use modified, "activated" or keygen images.
The image publisher scans and approves the golden image before the catalog item is published.

## Variables
| Variable | Default | Notes |
|---|---|---|
| `libvirt_uri` | `qemu+tcp://libvirt/system` | PoC libvirt container. On-prem: `qemu+ssh://provisioner@kvm-host/system` |
| `domain_type` | `kvm` | `qemu` (TCG) works but is far too slow for Windows |
| `golden_image` | `win2022-eval-golden.qcow2` | In the `golden` pool |
| `network_name` | `csir-windows-licensed` | |
