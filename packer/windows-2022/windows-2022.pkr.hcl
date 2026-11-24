# Windows Server 2022 (Desktop Experience) golden image for templates/windows-kvm.
# Media: the official Microsoft Evaluation Center ISO (180-day evaluation, legally re-armable for labs) - never
# third-party/modified media. For licensed production images point iso_url at the CSIR VLSC media and keep KMS activation.
# Pipeline: unattended install (autounattend.xml, virtio storage/net drivers from the virtio-win ISO) -> WinRM ->
# virtio guest tools + qemu-guest-agent -> cloudbase-init (NoCloud/ConfigDrive, runs the Coder agent bootstrap from
# user-data) -> RDP with NLA enabled -> Windows Update (optional) -> sysprep /generalize /oobe via cloudbase-init's
# unattend -> qcow2 -> published to the libvirt golden pool (build.sh). Requires a KVM host (TCG is far too slow).
packer {
  required_plugins {
    qemu = {
      source  = "github.com/hashicorp/qemu"
      version = "~> 1.1"
    }
    windows-update = {
      source  = "github.com/rgl/windows-update"
      version = "~> 0.16"
    }
  }
}

variable "iso_url" {
  type        = string
  default     = "https://go.microsoft.com/fwlink/p/?LinkID=2195280&clcid=0x409&culture=en-us&country=US"
  description = "Microsoft Evaluation Center: Windows Server 2022 64-bit en-US ISO"
}
variable "iso_checksum" {
  type        = string
  default     = "none"
  description = "sha256:<hash> of the downloaded ISO (record it from the Evaluation Center download / VLSC). 'none' only for first fetch."
}
variable "virtio_iso_url" {
  type    = string
  default = "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso"
}
variable "accelerator" {
  type    = string
  default = "kvm"
}
variable "admin_password" {
  type      = string
  sensitive = true
}
variable "answer_dir" {
  type = string
}
variable "output_dir" {
  type    = string
  default = "output"
}

source "qemu" "win2022" {
  iso_url           = var.iso_url
  iso_checksum      = var.iso_checksum
  accelerator       = var.accelerator
  machine_type      = "q35"
  cpus              = 4
  memory            = 4096
  disk_size         = "40G"
  format            = "qcow2"
  disk_compression  = true
  disk_interface    = "virtio"
  net_device        = "virtio-net"
  headless          = true
  vnc_bind_address  = "127.0.0.1"
  efi_boot          = true
  efi_firmware_code = "/usr/share/OVMF/OVMF_CODE_4M.fd"
  efi_firmware_vars = "/usr/share/OVMF/OVMF_VARS_4M.fd"
  cd_files          = ["${var.answer_dir}/autounattend.xml", "scripts/*"]
  cd_label          = "CSIRSETUP"
  qemuargs = [
    ["-drive", "file=${path.root}/virtio-win.iso,media=cdrom,index=3"],
  ]
  boot_wait        = "3s"
  boot_command     = ["<enter><wait><enter>"] # "Press any key to boot from CD"
  communicator     = "winrm"
  winrm_username   = "Administrator"
  winrm_password   = var.admin_password
  winrm_timeout    = "4h"
  shutdown_command = "C:\\Windows\\System32\\Sysprep\\sysprep.exe /generalize /oobe /shutdown /quiet /unattend:\"C:\\Program Files\\Cloudbase Solutions\\Cloudbase-Init\\conf\\Unattend.xml\""
  shutdown_timeout = "1h"
  output_directory = var.output_dir
  vm_name          = "win2022-eval-golden.qcow2"
}

build {
  sources = ["source.qemu.win2022"]

  provisioner "powershell" {
    scripts = ["scripts/virtio-guest-tools.ps1", "scripts/rdp-nla.ps1", "scripts/cloudbase-init.ps1"]
  }
  provisioner "windows-update" {
    search_criteria = "IsInstalled=0"
    filters         = ["exclude:$_.Title -like '*Preview*'", "include:$true"]
  }
  provisioner "powershell" {
    scripts = ["scripts/cleanup.ps1"]
  }
  post-processor "manifest" {
    output     = "${var.output_dir}/manifest.json"
    strip_path = true
  }
}
