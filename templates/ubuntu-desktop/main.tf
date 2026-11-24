# CSIR Dev Hub - Ubuntu Desktop (XFCE + xrdp for brokered RDP + noVNC + VS Code Server)
terraform {
  required_providers {
    coder  = { source = "coder/coder" }
    docker = { source = "kreuzwerker/docker" }
  }
}

provider "docker" {}
provider "coder" {}

data "coder_provisioner" "me" {}
data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

# ---------------------------------------------------------------- parameters
data "coder_parameter" "ubuntu_version" {
  name         = "ubuntu_version"
  display_name = "Ubuntu release"
  description  = "26.04 LTS preferred, 24.04 LTS fallback"
  default      = "24.04"
  mutable      = false
  option {
    name  = "Ubuntu 24.04 LTS (Noble)"
    value = "24.04"
  }
}

data "coder_parameter" "cpu" {
  name         = "cpu"
  display_name = "vCPU"
  description  = "CPU cores (hard limit). Default sized for the ~70-user chassis profile."
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
  default      = "4"
  mutable      = true
  validation {
    min = 2
    max = 8
  }
}

data "coder_parameter" "network_profile" {
  name         = "network_profile"
  display_name = "Network profile"
  description  = "dev = internet egress; isolated = no egress (only the Dev Hub control plane via gateway)"
  default      = "dev"
  mutable      = false
  option {
    name  = "dev (egress allowed)"
    value = "dev"
  }
  option {
    name  = "isolated (no egress)"
    value = "isolated"
  }
}

data "coder_parameter" "ide" {
  name         = "ide"
  display_name = "IDE access"
  description  = "browser = VS Code in the browser only (locked-down/kiosk); full = also VS Code Desktop via the Coder Remote extension, SSH and port-forward helpers"
  default      = "full"
  mutable      = true
  option {
    name  = "Browser + VS Code Desktop (Remote)"
    value = "full"
  }
  option {
    name  = "Browser only"
    value = "browser"
  }
}

locals {
  ide_full   = data.coder_parameter.ide.value == "full"
  username   = data.coder_workspace_owner.me.name
  ws_slug    = lower("${data.coder_workspace_owner.me.name}-${data.coder_workspace.me.name}")
  isolated   = data.coder_parameter.network_profile.value == "isolated"
  access_url = data.coder_workspace.me.access_url
  # In isolated mode the agent reaches Coder through a per-workspace gateway container.
  agent_url = local.isolated ? "http://csir-gw-${local.ws_slug}:3000" : local.access_url
  host_port = regex("^https?://[^/]+", local.access_url)
}

# ---------------------------------------------------------------- agent
resource "coder_agent" "main" {
  arch = data.coder_provisioner.me.arch
  os   = "linux"
  dir  = "/home/coder"

  # Built-in apps shown on the workspace page. vscode = "VS Code Desktop" button
  # (installs/uses the coder.coder-remote extension and opens a Remote-SSH session).
  display_apps {
    vscode                 = local.ide_full
    vscode_insiders        = false
    web_terminal           = true
    ssh_helper             = local.ide_full
    port_forwarding_helper = local.ide_full
  }
  env = {
    CSIR_DEVHUB_PROFILE = data.coder_parameter.network_profile.value
    GIT_AUTHOR_NAME     = coalesce(data.coder_workspace_owner.me.full_name, local.username)
    GIT_AUTHOR_EMAIL    = data.coder_workspace_owner.me.email
    GIT_COMMITTER_NAME  = coalesce(data.coder_workspace_owner.me.full_name, local.username)
    GIT_COMMITTER_EMAIL = data.coder_workspace_owner.me.email
  }
  startup_script = <<-EOT
    set -e
    # First start: seed the persistent home volume
    if [ ! -f ~/.csir_init_done ]; then
      cp -rT /etc/skel ~ || true
      cat > ~/WELCOME.md <<'EOM'
    # Welcome to the CSIR Software Development Hub
    Workspace: ${data.coder_workspace.me.name}  | Network profile: ${data.coder_parameter.network_profile.value}
    Your home directory (/home/coder) is a persistent volume.
    Injected secrets (if any) are mounted read-only at /run/csir-secrets.
    EOM
      touch ~/.csir_init_done
    fi
    # VS Code Server (code-server) in the browser
    code-server --auth none --bind-addr 127.0.0.1:13337 --disable-telemetry >/tmp/code-server.log 2>&1 &
    # Desktop: xrdp :3389 (reachable only on the private workspace network -> Guacamole broker / coder port-forward)
    # and TigerVNC + noVNC :6080 (browser desktop through the Coder app proxy)
    /usr/local/bin/start-desktop.sh
  EOT

  metadata {
    display_name = "CPU Usage"
    key          = "0_cpu_usage"
    script       = "coder stat cpu"
    interval     = 10
    timeout      = 1
  }
  metadata {
    display_name = "RAM Usage"
    key          = "1_ram_usage"
    script       = "coder stat mem"
    interval     = 10
    timeout      = 1
  }
  metadata {
    display_name = "Home Disk"
    key          = "3_home_disk"
    script       = "coder stat disk --path $${HOME}"
    interval     = 60
    timeout      = 1
  }
}

resource "coder_app" "novnc" {
  agent_id     = coder_agent.main.id
  slug         = "novnc"
  display_name = "Desktop (noVNC)"
  url          = "http://localhost:6080/vnc.html?autoconnect=1&resize=remote&path=@${data.coder_workspace_owner.me.name}/${data.coder_workspace.me.name}.main/apps/novnc/websockify"
  icon         = "/icon/desktop.svg"
  subdomain    = false
  share        = "owner"
  healthcheck {
    url       = "http://localhost:6080/"
    interval  = 10
    threshold = 12
  }
}

resource "coder_app" "code-server" {
  agent_id     = coder_agent.main.id
  slug         = "code-server"
  display_name = "VS Code (browser)"
  url          = "http://localhost:13337/?folder=/home/coder"
  icon         = "/icon/code.svg"
  subdomain    = false
  share        = "owner"
  healthcheck {
    url       = "http://localhost:13337/healthz"
    interval  = 5
    threshold = 6
  }
}

# ---------------------------------------------------------------- storage
resource "docker_volume" "home" {
  name = "coder-${data.coder_workspace.me.id}-home"
  lifecycle { ignore_changes = all }
  labels {
    label = "coder.owner"
    value = local.username
  }
  labels {
    label = "coder.workspace_id"
    value = data.coder_workspace.me.id
  }
}

# Per-user secrets volume `csir-secrets-<user>` is created/written by the CSIR Dev Hub portal and shared by all of the
# user's workspaces, so it is NOT a Terraform-managed resource (deleting one workspace must not delete it). Docker
# creates it on first mount if the portal has not yet.
removed {
  from = docker_volume.secrets
  lifecycle {
    destroy = false
  }
}

locals {
  secrets_volume = "csir-secrets-${local.username}"
}

# ---------------------------------------------------------------- network isolation
# Every workspace gets its own bridge network: workspaces cannot reach each other.
resource "docker_network" "ws" {
  name     = "csir-ws-${local.ws_slug}"
  internal = local.isolated
  labels {
    label = "org.csir.devhub.profile"
    value = data.coder_parameter.network_profile.value
  }
}

# Isolated profile: control-plane-only gateway (TCP relay to Coder; no other egress).
resource "docker_container" "gateway" {
  count   = local.isolated && data.coder_workspace.me.start_count > 0 ? 1 : 0
  image   = "alpine/socat:latest"
  name    = "csir-gw-${local.ws_slug}"
  command = ["TCP-LISTEN:3000,fork,reuseaddr", "TCP:${replace(replace(local.host_port, "http://", ""), "https://", "")}"]
  networks_advanced { name = docker_network.ws.name }
  networks_advanced { name = "bridge" }
  memory = 32
}

# ---------------------------------------------------------------- workspace container
resource "docker_container" "workspace" {
  count      = data.coder_workspace.me.start_count
  image      = "csir-devhub/ubuntu-desktop:${data.coder_parameter.ubuntu_version.value}"
  name       = "coder-${local.ws_slug}"
  hostname   = data.coder_workspace.me.name
  shm_size   = 512
  user       = "coder"
  entrypoint = ["sh", "-c", replace(coder_agent.main.init_script, local.access_url, local.agent_url)]
  env        = ["CODER_AGENT_TOKEN=${coder_agent.main.token}", "CODER_AGENT_URL=${local.agent_url}"]

  memory     = data.coder_parameter.memory.value * 1024
  cpu_shares = data.coder_parameter.cpu.value * 1024
  cpu_set    = join(",", range(0, data.coder_parameter.cpu.value))

  networks_advanced { name = docker_network.ws.name }

  volumes {
    container_path = "/home/coder"
    volume_name    = docker_volume.home.name
  }
  volumes {
    container_path = "/run/csir-secrets"
    volume_name    = local.secrets_volume
    read_only      = true
  }

  labels {
    label = "coder.owner"
    value = local.username
  }
  labels {
    label = "coder.workspace_name"
    value = data.coder_workspace.me.name
  }
  labels {
    label = "org.csir.devhub.template"
    value = "ubuntu-desktop"
  }
  depends_on = [docker_container.gateway]
}

resource "coder_metadata" "info" {
  count       = data.coder_workspace.me.start_count
  resource_id = docker_container.workspace[0].id
  item {
    key   = "image"
    value = "csir-devhub/ubuntu-desktop:${data.coder_parameter.ubuntu_version.value}"
  }
  item {
    key   = "network profile"
    value = data.coder_parameter.network_profile.value
  }
  item {
    key   = "rdp"
    value = "brokered via Guacamole (portal) or: coder port-forward ${data.coder_workspace.me.name} --tcp 3389:3389"
  }
  item {
    key   = "limits"
    value = "${data.coder_parameter.cpu.value} vCPU / ${data.coder_parameter.memory.value} GB"
  }
}
