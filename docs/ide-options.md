# IDE and desktop access options - CSIR Dev Hub

Status key: **Verified** (tested on the PoC box), **Ready** (configured, needs a client on the user's PC), **Planned**.

## 1. VS Code in the browser (code-server) - Verified
* Baked into `images/ubuntu-server` (code-server 4.146.0), started by the agent `startup_script` on `127.0.0.1:13337` with `--auth none`
  (the user is already authenticated to Coder; the app is only reachable through Coder's authenticated proxy).
* Exposed by `coder_app "code-server"`: path-based (`subdomain = false`, no wildcard DNS needed), `share = "owner"`, healthcheck `/healthz`.
* URL pattern: `http://<HOST>:3000/@<user>/<workspace>.main/apps/code-server/?folder=/home/coder`
* Verified 2026-11-24: app health `healthy`, proxied request returns HTTP 200 with the VS Code workbench.
* Extensions come from Open VSX. For air-gapped on-prem use, point `EXTENSIONS_GALLERY` at an internal Open VSX mirror.

## 2. VS Code Desktop (Coder Remote extension) - Ready
* `coder_agent.display_apps.vscode = true` shows a **VS Code Desktop** button on the workspace page. It opens
  `vscode://coder.coder-remote/open?...`, which installs/uses the `coder.coder-remote` extension, authenticates with a Coder
  token and connects over Coder's WireGuard tunnel (Remote-SSH under the hood).
* Template parameter `ide`: `full` (browser + desktop + SSH/port-forward helpers) or `browser` (browser-only / kiosk; hides
  the desktop button and SSH helpers). Mutable, so a user/admin can switch it with `coder update <ws> --parameter ide=browser`.
* Verified via API: agent `display_apps = [vscode, ssh_helper, port_forwarding_helper, web_terminal]`. The click-through needs
  VS Code installed on the user's machine with network access to the Coder URL (not possible from the PoC box itself).
* Manual alternative: `coder config-ssh` then VS Code Remote-SSH to host `coder.<workspace>`.

## 3. SSH / terminal - Verified
`coder ssh demo-ubuntu`, the browser **Terminal** button (`web_terminal`), or `coder config-ssh` + any SSH client.

## 4. JetBrains (IntelliJ, PyCharm, GoLand, ...) - Planned
Options, in order of preference:
1. registry module (Toolbox / Gateway):
   ```hcl
   module "jetbrains" {
     source   = "registry.coder.com/coder/jetbrains/coder"
     version  = "~> 1.0"
     agent_id = coder_agent.main.id
     folder   = "/home/coder"
     # default = ["IU", "PY"]  # pre-select IDEs
   }
   ```
2. JetBrains Gateway with the Coder plugin (connects over SSH; nothing to add to the template).
Air-gapped: mirror the module into an internal Terraform registry and pre-download IDE backends into the image.

## 5. Linux desktops - Ubuntu Desktop template (next path)
* `images/ubuntu-desktop`: XFCE + **xrdp** (3389) + TigerVNC + **noVNC** (6080) + code-server.
* Access paths:
  * **Brokered RDP via Apache Guacamole** (portal "Desktop (RDP)" button; guacd joins the workspace network on demand; the
    portal mints a signed, short-lived Guacamole JSON-auth token - users never see RDP passwords).
  * **noVNC** in the browser as a `coder_app` (no broker needed).
  * **Native RDP client**: `coder port-forward <ws> --tcp 3389:3389`, then mstsc/Remmina to `localhost:3389`.
* Alternatives evaluated: KasmVNC (registry module `kasmvnc`) - better image quality/clipboard than noVNC, drop-in later.

## 6. Windows desktops - KVM path
* `/dev/kvm` is present on the PoC host (and passes through to containers), so real Windows VMs are technically possible.
* Path: libvirt/QEMU template (`templates/windows-kvm`, terraform `dmacvicar/libvirt`) or the `dockur/windows` container (QEMU+KVM
  inside Docker), RDP brokered through Guacamole; Coder registry modules `windows-rdp` (web RDP via Devolutions Gateway) and
  `local-windows-rdp` are alternatives.
* The Coder agent runs inside the Windows guest (`os = "windows"`, init script via cloud-init/autounattend).
* Licensing: the `windows-licensed` network profile + licence-seat pool in the portal (stub broker today).
* PoC constraint: the box has ~4-9 GB RAM free, so a Windows 11 VM (min 4 GB, realistic 8 GB) is not run here; see
  `docs/extending-kvm.md`.
