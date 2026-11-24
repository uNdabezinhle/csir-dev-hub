# ubuntu-server template

Ubuntu 24.04 / 26.04 Docker workspace for the CSIR Dev Hub.

| Feature | How |
|---|---|
| VS Code in browser | `coder_app.code-server` -> code-server on `localhost:13337` (baked into image, `--auth none` because Coder already authenticated the user; path-based app, `share = owner`) |
| VS Code Desktop | `display_apps.vscode = true` -> "VS Code Desktop" button (Coder Remote extension `coder.coder-remote`). Hidden when parameter `ide = browser` |
| Terminal / SSH | `web_terminal`, `ssh_helper`; CLI: `coder ssh <workspace>` or `coder config-ssh` then `ssh coder.<workspace>` |
| Port forwarding | `port_forwarding_helper`; CLI: `coder port-forward <ws> --tcp 8000:8000` |
| Persistence | Docker volume `coder-<workspace-id>-home` mounted at `/home/coder` (survives stop/start/rebuild) |
| Secrets | Per-user volume `csir-secrets-<user>` mounted read-only at `/run/csir-secrets` (written by the portal) |
| Network | Own bridge network per workspace; `network_profile = isolated` makes it `internal` with a control-plane-only gateway |
| Limits | `cpu` (cpuset + shares) and `memory` (hard limit) parameters |

Parameters: `ubuntu_version` (24.04 | 26.04, immutable), `cpu`, `memory`, `network_profile` (immutable), `ide` (full | browser).
Image: `images/ubuntu-server` (`scripts/build-images.sh`). See `docs/ide-options.md` for JetBrains and desktop/RDP options.
