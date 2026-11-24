# CSIR Dev Hub - Ubuntu VM on KVM/libvirt (full OS: kernel modules, systemd, Docker-in-VM, driver work).
# Proves the libvirt path of the annex (KVM hosts managed by the same Coder control plane):
#   golden image (golden pool, published by scripts/kvm-publish-image.sh or packer/ubuntu-golden)
#   -> copy-on-write system disk per workspace (persists across stop/start) -> cloud-init seed injects the Coder agent
#   -> libvirt network per network profile (csir-dev NAT / csir-isolated no-forward).
# On the PoC box guests run under QEMU TCG (domain_type = "qemu") because nested KVM is broken there; on a real KVM
# host set domain_type = "kvm" (template variable) - nothing else changes.
terraform {
  required_providers {
    coder   = { source = "coder/coder" }
    libvirt = { source = "dmacvicar/libvirt", version = "0.8.3" }
    random  = { source = "hashicorp/random" }
  }
}

variable "libvirt_uri" {
  description = "libvirt URI reachable from the Coder provisioner (PoC: libvirt container on the private network)"
  type        = string
  default     = "qemu+tcp://libvirt/system"
}
variable "domain_type" {
  description = "kvm on real KVM hosts; qemu (TCG software emulation) where /dev/kvm is unusable"
  type        = string
  default     = "qemu"
}
variable "golden_image" {
  description = "Golden image in the 'golden' pool: ubuntu-24.04-golden.qcow2 (packer/ubuntu-golden) or ubuntu-24.04-base.qcow2 (plain cloud image)"
  type        = string
  default     = "ubuntu-24.04-golden.qcow2"
}

provider "libvirt" {
  uri = var.libvirt_uri
}
provider "coder" {}

data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

data "coder_parameter" "cpu" {
  name         = "cpu"
  display_name = "vCPU"
  type         = "number"
  default      = "2"
  mutable      = true
  validation {
    min = 1
    max = 4
  }
}
data "coder_parameter" "memory" {
  name         = "memory"
  display_name = "Memory (GB)"
  type         = "number"
  default      = "2"
  mutable      = true
  validation {
    min = 1
    max = 8
  }
}
data "coder_parameter" "disk" {
  name         = "disk"
  display_name = "System disk (GB)"
  type         = "number"
  default      = "16"
  mutable      = false
  validation {
    min = 10
    max = 100
  }
}
data "coder_parameter" "network_profile" {
  name         = "network_profile"
  display_name = "Network profile"
  default      = "dev"
  mutable      = false
  option {
    name  = "dev (NAT egress)"
    value = "dev"
  }
  option {
    name  = "isolated (no egress)"
    value = "isolated"
  }
}
data "coder_parameter" "code_server" {
  name         = "code_server"
  display_name = "Install VS Code Server (code-server) in the VM"
  type         = "bool"
  default      = "true"
  mutable      = true
}

locals {
  name    = lower("csir-${data.coder_workspace_owner.me.name}-${data.coder_workspace.me.name}")
  network = data.coder_parameter.network_profile.value == "isolated" ? "csir-isolated" : "csir-dev"
}

resource "coder_agent" "main" {
  os   = "linux"
  arch = "amd64"
  dir  = "/home/coder"
  display_apps {
    vscode          = true
    web_terminal    = true
    ssh_helper      = true
    vscode_insiders = false
  }
  startup_script = <<-EOT
    set -e
    if [ "${data.coder_parameter.code_server.value}" = "true" ]; then
      if [ ! -x /home/coder/.local/code-server/bin/code-server ]; then
        curl -fsSL https://code-server.dev/install.sh | sh -s -- --method=standalone --prefix=/home/coder/.local/code-server >/tmp/code-server-install.log 2>&1
      fi
      /home/coder/.local/code-server/bin/code-server --auth none --bind-addr 127.0.0.1:13337 --disable-telemetry >/tmp/code-server.log 2>&1 &
    fi
  EOT
  metadata {
    display_name = "CPU"
    key          = "cpu"
    script       = "coder stat cpu"
    interval     = 15
    timeout      = 2
  }
  metadata {
    display_name = "Memory"
    key          = "mem"
    script       = "coder stat mem"
    interval     = 15
    timeout      = 2
  }
  metadata {
    display_name = "Kernel"
    key          = "kernel"
    script       = "uname -r"
    interval     = 3600
    timeout      = 2
  }
}

resource "coder_app" "code-server" {
  count        = data.coder_parameter.code_server.value == "true" ? 1 : 0
  agent_id     = coder_agent.main.id
  slug         = "code-server"
  display_name = "VS Code (browser)"
  url          = "http://localhost:13337/?folder=/home/coder"
  icon         = "/icon/code.svg"
  subdomain    = false
  share        = "owner"
  healthcheck {
    url       = "http://localhost:13337/healthz"
    interval  = 10
    threshold = 30
  }
}

# Persistent system disk: CoW overlay on the read-only golden image (kept across stop/start, removed on delete).
resource "libvirt_volume" "system" {
  name             = "${local.name}-system.qcow2"
  pool             = "default"
  base_volume_name = var.golden_image
  base_volume_pool = "golden"
  size             = data.coder_parameter.disk.value * 1024 * 1024 * 1024
  format           = "qcow2"
  # a newer golden image applies to NEW workspaces; existing disks keep their base (no data loss on template updates)
  lifecycle {
    ignore_changes = [base_volume_name, base_volume_pool]
  }
}

# cloud-init: coder user + the Coder agent as a systemd service (token rotates per build, so the seed is per build).
resource "libvirt_cloudinit_disk" "init" {
  name = "${local.name}-seed-${substr(md5(coder_agent.main.token), 0, 8)}.iso"
  pool = "default"
  meta_data = jsonencode({
    "instance-id"    = "${local.name}-${substr(md5(coder_agent.main.token), 0, 8)}"
    "local-hostname" = data.coder_workspace.me.name
  })
  user_data = <<-EOT
    #cloud-config
    hostname: ${data.coder_workspace.me.name}
    users:
      - name: coder
        shell: /bin/bash
        groups: [sudo]
        sudo: "ALL=(ALL) NOPASSWD:ALL"
        lock_passwd: false
    # per-workspace RDP credential (xrdp, brokered by Guacamole); SSH stays key/agent-only
    ssh_pwauth: false
    chpasswd:
      expire: false
      users:
        - name: coder
          password: ${random_password.rdp.result}
          type: text
    write_files:
      - path: /opt/coder/init.sh
        permissions: "0755"
        encoding: b64
        content: ${base64encode(coder_agent.main.init_script)}
      - path: /etc/systemd/system/coder-agent.service
        content: |
          [Unit]
          Description=Coder agent
          After=network-online.target
          Wants=network-online.target
          [Service]
          User=coder
          Environment=CODER_AGENT_TOKEN=${coder_agent.main.token}
          ExecStart=/opt/coder/init.sh
          Restart=always
          RestartSec=10
          [Install]
          WantedBy=multi-user.target
    runcmd:
      - [systemctl, daemon-reload]
      - [systemctl, enable, coder-agent.service]
      # restart, not just start: on rebuilds the unit from the previous boot is already running with the old token
      - [systemctl, restart, coder-agent.service]
      - [sh, -c, "echo CSIR-VM-READY > /dev/ttyS0"]
  EOT
}

resource "libvirt_domain" "vm" {
  count     = data.coder_workspace.me.start_count
  name      = local.name
  type      = var.domain_type
  vcpu      = data.coder_parameter.cpu.value
  memory    = data.coder_parameter.memory.value * 1024
  machine   = "pc" # i440fx: the provider attaches the cloud-init seed as an IDE cdrom (q35 has no IDE; see windows-kvm for the xslt fix)
  cloudinit = libvirt_cloudinit_disk.init.id
  autostart = true # running workspaces come back after a libvirt host restart (stopped ones have no domain)

  disk {
    volume_id = libvirt_volume.system.id
  }
  network_interface {
    network_name   = local.network
    wait_for_lease = false
  }
  console {
    type        = "pty"
    target_type = "serial"
    target_port = "0"
  }
  graphics {
    type        = "vnc"
    listen_type = "none"
  }
}

# Local password for brokered RDP (xrdp). Kept in Terraform state only; the portal reads it from the sensitive
# metadata item below and hands it to Guacamole inside the signed connection token.
resource "random_password" "rdp" {
  length  = 24
  special = false
}

resource "coder_metadata" "vm" {
  count       = data.coder_workspace.me.start_count
  resource_id = libvirt_domain.vm[0].id
  item {
    key   = "hypervisor"
    value = var.domain_type == "kvm" ? "KVM" : "QEMU TCG (software emulation - PoC box)"
  }
  item {
    key   = "limits"
    value = "${data.coder_parameter.cpu.value} vCPU / ${data.coder_parameter.memory.value} GB / ${data.coder_parameter.disk.value} GB disk"
  }
  item {
    key   = "network"
    value = local.network
  }
  item {
    key       = "rdp_password"
    value     = random_password.rdp.result
    sensitive = true
  }
}
