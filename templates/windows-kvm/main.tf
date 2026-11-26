# CSIR Dev Hub - Windows Server 2022 / Windows 11 workspace on KVM via libvirt (windows-licensed profile).
#   golden image  : packer/windows-2022 (Microsoft Evaluation Center media, sysprepped, cloudbase-init, virtio, RDP+NLA)
#                   published to the libvirt "golden" pool as win2022-eval-golden.qcow2
#   per workspace : CoW system disk + persistent data disk, cloudbase-init seed (Coder agent + local RDP user with a
#                   per-workspace random password), q35 + OVMF Secure Boot + swtpm TPM 2.0, csir-windows-licensed network
#   access        : RDP only via the portal -> Guacamole -> libvirt DNAT reachable only from guacd (no 3389 on the LAN),
#                   or `coder port-forward <ws> --tcp 3389:3389` over the Coder tunnel; licence seat checked out by the
#                   portal's licence broker on create, returned on delete.
# PoC box status: pushed to Coder but NOT buildable there - nested KVM crashes the box kernel (kvm_spurious_fault in
# vmx_vcpu_create) and Windows under TCG is impractically slow, so no golden image was built. See docs/extending-kvm.md.
terraform {
  required_providers {
    coder   = { source = "coder/coder" }
    libvirt = { source = "dmacvicar/libvirt", version = "0.8.3" }
    random  = { source = "hashicorp/random" }
  }
}

variable "libvirt_uri" {
  description = "libvirt URI of the KVM host (PoC: libvirt container; production: qemu+ssh://provisioner@kvm-host/system)"
  type        = string
  default     = "qemu+tcp://libvirt/system"
}
variable "domain_type" {
  description = "kvm (required for Windows in practice); qemu = TCG software emulation"
  type        = string
  default     = "kvm"
}
variable "golden_image" {
  description = "Golden image volume in the 'golden' pool (packer/windows-2022/build.sh)"
  type        = string
  default     = "win2022-eval-golden.qcow2"
}
variable "network_name" {
  description = "libvirt network implementing the windows-licensed profile (KMS + RDP gateway only)"
  type        = string
  default     = "csir-windows-licensed"
}
variable "ovmf_code" {
  type    = string
  default = "/usr/share/OVMF/OVMF_CODE_4M.secboot.fd"
}
variable "ovmf_vars_template" {
  type    = string
  default = "/usr/share/OVMF/OVMF_VARS_4M.ms.fd"
}

provider "libvirt" {
  uri = var.libvirt_uri
}
provider "coder" {}

data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

data "coder_parameter" "devhub_request_id" {
  name         = "devhub_request_id"
  display_name = "Dev Hub provisioning request ID"
  description  = "Set by the Dev Hub portal. Leave blank when creating a workspace directly."
  type         = "string"
  default      = ""
  mutable      = false
}

data "coder_parameter" "cpu" {
  name         = "cpu"
  display_name = "vCPU"
  type         = "number"
  default      = "2"
  mutable      = true
  validation {
    min = 2
    max = 8
  }
}
data "coder_parameter" "memory" {
  name         = "memory"
  display_name = "Memory (GB)"
  type         = "number"
  default      = "4"
  mutable      = true
  validation {
    min = 4
    max = 16
  }
}
data "coder_parameter" "data_disk" {
  name         = "data_disk"
  display_name = "Data disk D: (GB)"
  type         = "number"
  default      = "40"
  mutable      = false
  validation {
    min = 10
    max = 200
  }
}
data "coder_parameter" "network_profile" {
  name         = "network_profile"
  display_name = "Network profile"
  default      = "windows-licensed"
  mutable      = false
  option {
    name  = "windows-licensed (KMS + brokered RDP)"
    value = "windows-licensed"
  }
}

locals {
  name = lower("csir-${data.coder_workspace_owner.me.name}-${data.coder_workspace.me.name}")
}

# Per-workspace local RDP account password (stored only in Coder's Terraform state; shown to the owner as sensitive
# metadata and used by the portal to mint the Guacamole connection - users never need to type it).
resource "random_password" "rdp" {
  length           = 20
  special          = true
  override_special = "!#%-_=+"
  min_upper        = 2
  min_lower        = 2
  min_numeric      = 2
  min_special      = 1
}

resource "coder_agent" "main" {
  os   = "windows"
  arch = "amd64"
  display_apps {
    vscode                 = true
    web_terminal           = true
    ssh_helper             = true
    port_forwarding_helper = true
  }
  metadata {
    display_name = "CPU Usage"
    key          = "cpu"
    script       = "coder stat cpu"
    interval     = 10
    timeout      = 1
  }
  metadata {
    display_name = "Memory Usage"
    key          = "mem"
    script       = "coder stat mem"
    interval     = 10
    timeout      = 1
  }
}

# Persistent system disk: copy-on-write overlay of the golden image (survives stop/start, removed on delete).
resource "libvirt_volume" "system" {
  name             = "${local.name}-system.qcow2"
  pool             = "default"
  base_volume_name = var.golden_image
  base_volume_pool = "golden"
  format           = "qcow2"
}

# Separate persistent data disk (D:) for user files.
resource "libvirt_volume" "data" {
  name   = "${local.name}-data.qcow2"
  pool   = "default"
  size   = data.coder_parameter.data_disk.value * 1024 * 1024 * 1024
  format = "qcow2"
}

# cloudbase-init user-data (NoCloud): Coder agent token + bootstrap, local RDP user, RDP firewall scoped to the gateway.
resource "libvirt_cloudinit_disk" "init" {
  name = "${local.name}-seed-${substr(md5(coder_agent.main.token), 0, 8)}.iso"
  pool = "default"
  meta_data = jsonencode({
    "instance-id"    = "${local.name}-${substr(md5(coder_agent.main.token), 0, 8)}"
    "local-hostname" = substr(replace(data.coder_workspace.me.name, "_", "-"), 0, 15)
  })
  user_data = templatefile("${path.module}/cloudbase-init.ps1.tftpl", {
    init_script  = coder_agent.main.init_script
    token        = coder_agent.main.token
    rdp_user     = "coder"
    rdp_password = random_password.rdp.result
  })
}

resource "libvirt_domain" "vm" {
  count     = data.coder_workspace.me.start_count
  name      = local.name
  type      = var.domain_type
  vcpu      = data.coder_parameter.cpu.value
  memory    = data.coder_parameter.memory.value * 1024
  machine   = "q35"
  firmware  = var.ovmf_code
  cloudinit = libvirt_cloudinit_disk.init.id
  autostart = true # running workspaces come back after a libvirt host restart (stopped ones have no domain)

  nvram {
    file     = "/var/lib/libvirt/qemu/nvram/${local.name}_VARS.fd"
    template = var.ovmf_vars_template
  }
  cpu {
    mode = var.domain_type == "kvm" ? "host-passthrough" : "custom"
  }
  tpm {
    backend_type    = "emulator"
    backend_version = "2.0"
  }
  disk {
    volume_id = libvirt_volume.system.id
  }
  disk {
    volume_id = libvirt_volume.data.id
  }
  network_interface {
    network_name   = var.network_name
    wait_for_lease = false
  }
  video {
    type = "qxl"
  }
  graphics {
    type        = "vnc"
    listen_type = "none"
  }
  xml {
    xslt = file("${path.module}/cdrom-sata.xsl")
  }
}

resource "coder_metadata" "vm" {
  count       = data.coder_workspace.me.start_count
  resource_id = libvirt_domain.vm[0].id
  item {
    key   = "image"
    value = var.golden_image
  }
  item {
    key   = "limits"
    value = "${data.coder_parameter.cpu.value} vCPU / ${data.coder_parameter.memory.value} GB / D: ${data.coder_parameter.data_disk.value} GB"
  }
  item {
    key   = "rdp_user"
    value = "coder"
  }
  item {
    key       = "rdp_password"
    value     = random_password.rdp.result
    sensitive = true
  }
  item {
    key   = "access"
    value = "RDP via the portal (Guacamole) - no 3389 on the LAN"
  }
}
