# CSIR Dev Hub - Windows 11 / Server 2025 on KVM via libvirt (DRAFT - not pushed in the PoC).
# Requirements (production host): Ubuntu 24.04/26.04 with /dev/kvm, libvirtd, a sysprepped Windows golden qcow2 with
# cloudbase-init (installs/starts the Coder agent from user-data) and a licence/KMS reachable on the windows-licensed network.
# The Coder provisioner reaches libvirtd via LIBVIRT_DEFAULT_URI (e.g. qemu+ssh://coder@kvm-host/system).
terraform {
  required_providers {
    coder   = { source = "coder/coder" }
    libvirt = { source = "dmacvicar/libvirt", version = "0.8.3" }
  }
}

variable "libvirt_uri" {
  description = "libvirt connection URI for the KVM host"
  type        = string
  default     = "qemu:///system"
}
variable "golden_image" {
  description = "Path (on the KVM host) of the sysprepped Windows golden qcow2"
  type        = string
  default     = "/var/lib/libvirt/images/golden/win11-23h2-csir.qcow2"
}
variable "storage_pool" {
  type    = string
  default = "default"
}
variable "network_name" {
  description = "libvirt network implementing the windows-licensed profile (KMS + RDP gateway only)"
  type        = string
  default     = "csir-windows-licensed"
}

provider "libvirt" {
  uri = var.libvirt_uri
}
provider "coder" {}

data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

data "coder_parameter" "edition" {
  name         = "edition"
  display_name = "Windows edition"
  default      = "win11"
  mutable      = false
  option {
    name  = "Windows 11 Enterprise"
    value = "win11"
  }
  option {
    name  = "Windows Server 2025"
    value = "ws2025"
  }
}
data "coder_parameter" "cpu" {
  name         = "cpu"
  display_name = "vCPU"
  type         = "number"
  default      = "4"
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
  default      = "8"
  mutable      = true
  validation {
    min = 4
    max = 16
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
}

# RDP inside the guest is reachable only on the libvirt network; access is brokered by Guacamole (portal) or
# `coder port-forward <ws> --tcp 3389:3389` over the Coder tunnel.
resource "coder_app" "rdp-info" {
  agent_id     = coder_agent.main.id
  slug         = "rdp"
  display_name = "Desktop (RDP via portal)"
  url          = "http://localhost:3389"
  icon         = "/icon/desktop.svg"
  subdomain    = false
  share        = "owner"
}

# Persistent system disk: copy-on-write overlay of the golden image (survives stop/start).
resource "libvirt_volume" "system" {
  name           = "${local.name}-system.qcow2"
  pool           = var.storage_pool
  base_volume_id = var.golden_image
  format         = "qcow2"
  lifecycle { ignore_changes = all }
}

# Separate persistent data disk (D:) for user files.
resource "libvirt_volume" "data" {
  name   = "${local.name}-data.qcow2"
  pool   = var.storage_pool
  size   = 50 * 1024 * 1024 * 1024
  format = "qcow2"
  lifecycle { ignore_changes = all }
}

# cloudbase-init user-data: sets the agent token/URL and installs the Coder agent as a service.
resource "libvirt_cloudinit_disk" "init" {
  name = "${local.name}-init.iso"
  pool = var.storage_pool
  user_data = templatefile("${path.module}/cloudbase-init.ps1.tftpl", {
    init_script = coder_agent.main.init_script
    token       = coder_agent.main.token
  })
}

resource "libvirt_domain" "vm" {
  count     = data.coder_workspace.me.start_count
  name      = local.name
  vcpu      = data.coder_parameter.cpu.value
  memory    = data.coder_parameter.memory.value * 1024
  machine   = "q35"
  firmware  = "/usr/share/OVMF/OVMF_CODE_4M.secboot.fd"
  cloudinit = libvirt_cloudinit_disk.init.id

  cpu {
    mode = "host-passthrough"
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
  graphics {
    type        = "spice"
    listen_type = "none"
  }
}

resource "coder_metadata" "info" {
  count       = data.coder_workspace.me.start_count
  resource_id = libvirt_domain.vm[0].id
  item {
    key   = "edition"
    value = data.coder_parameter.edition.value
  }
  item {
    key   = "limits"
    value = "${data.coder_parameter.cpu.value} vCPU / ${data.coder_parameter.memory.value} GB"
  }
}
