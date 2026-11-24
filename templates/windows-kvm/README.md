# windows-kvm template (DRAFT)
Windows 11 Enterprise / Server 2025 as a **KVM/QEMU VM via libvirt** (`dmacvicar/libvirt`), Coder agent inside the guest
(cloudbase-init user-data), q35 + OVMF Secure Boot + emulated TPM 2.0 (Windows 11 requirement), CoW overlay of a sysprepped
golden image + separate persistent data disk, `windows-licensed` libvirt network. RDP is never published: brokered through
Guacamole by the portal or tunnelled with `coder port-forward`.

Status: `terraform validate` passes; **not pushed to Coder in the PoC** because the PoC box has no libvirtd/golden image
and only ~4-8 GB free RAM. `/dev/kvm` is present and usable from containers (KVM API version 12; `KVM_CREATE_VM` succeeds). A full guest boot was not demonstrated on the PoC box.
Alternative quick path for demos: `dockurr/windows` container (QEMU+KVM inside Docker, `--device /dev/kvm`, RDP on 3389 on a
private network) - see docs/extending-kvm.md.
