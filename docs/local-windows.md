# Running CSIR Dev Hub locally on a Windows 11 laptop (Docker Desktop)

Single-user local run of the full control plane at `http://localhost:<port>`. Project folder used on the
Technical Lead's laptop: `D:\CSIR\Projects\devhub`.

## Prerequisites
- Docker Desktop (WSL2 backend) running. Its memory cap comes from `%USERPROFILE%\.wslconfig` (`[wsl2] memory=`);
  8 GB is enough for the stack + one `ubuntu-server` workspace (~3.5 GB used); give it 12 GB+ for desktop/Kali workspaces too.
- Git for Windows (provides `bash.exe`, `openssl`, `curl`) and Python 3.10+ from python.org (`python.exe`/`py.exe`).
- Free ports 3000, 3001, 8080, 8088, 8089, 9090 (else change them in `.env`).
- No KVM: Docker Desktop on Windows has no `/dev/kvm`, so the `ubuntu-vm` / `windows-kvm` templates and the libvirt
  service stay off (`KVM_ENABLED=false`).

## Get the code
```powershell
git clone -c core.autocrlf=false https://github.com/uNdabezinhle/csir-dev-hub.git D:\CSIR\Projects\devhub
cd D:\CSIR\Projects\devhub
```
`.gitattributes` keeps shell/python/terraform files LF on Windows.

## Start / stop
```powershell
cd D:\CSIR\Projects\devhub
powershell -ExecutionPolicy Bypass -File .\scripts\start-desktop.ps1   # first run: secrets, image builds, bootstrap (15-30 min)
powershell -ExecutionPolicy Bypass -File .\scripts\stop-desktop.ps1    # stop (volumes/data kept)
powershell -ExecutionPolicy Bypass -File .\scripts\devhub.ps1 scripts/dc.sh ps          # any bash script
powershell -ExecutionPolicy Bypass -File .\scripts\devhub.ps1 scripts/test-mfa.py       # any python script
```
`start-desktop.ps1` is idempotent: it copies `.env.desktop.example` to `.env` (first run), generates
`secrets\devhub.env` (Git Bash + openssl; ACL restricted to the current user with `icacls`), runs `scripts/start.sh`
(compose up, identity bootstrap, Coder first user + portal token, Guacamole bootstrap, workspace image builds for
`DESKTOP_TEMPLATES`, template push, `admin/demo-ubuntu` workspace).

## URLs
| Service | URL | Login |
|---|---|---|
| Portal / admin console | http://localhost:8088 | `admin` / `CODER_ADMIN_PASSWORD`, or "Sign in with CSIR SSO" |
| Coder (VS Code in the browser) | http://localhost:3000 | `admin@csir.local` / `CODER_ADMIN_PASSWORD`, or SSO |
| Keycloak admin | http://localhost:8080/admin/master/console/ | `admin` / `KEYCLOAK_ADMIN_PASSWORD` (realm `csir`) |
| Grafana | http://localhost:3001 | `admin` / `GRAFANA_ADMIN_PASSWORD` |
| Prometheus | http://localhost:9090 | - |
| Guacamole | http://localhost:8089/guacamole/ | via portal RDP button; `guacadmin` / `GUACADMIN_PASSWORD` |

SSO demo users (LDAP, forced TOTP enrolment on first login): see `identity/roles.json`; password `LDAP_DEMO_PASSWORD`.
All keys are in `secrets\devhub.env` (never committed).

## How the Docker Desktop overlay works (`docker-compose.desktop.yml`, `DESKTOP_MODE=true`)
- **One issuer for browser and containers.** `HOST_IP=localhost`, so the Keycloak issuer is
  `http://localhost:8080/realms/csir`. Inside `coder` and `portal`, a `socat` relay bound to the container's own
  `127.0.0.1:8080` forwards to `keycloak:8080` (the portal also relays 3000/3001/8089/9090). Browser and Coder see the
  same issuer string, so there is no issuer mismatch (the failure that crash-looped Coder in public mode).
- **Workspaces reach Coder via `host.docker.internal`.** Templates rewrite a `localhost`/`127.0.0.1` access URL to
  `host.docker.internal` for the agent (no-op on a server whose access URL is a LAN IP/DNS name).
- **Docker socket.** Docker Desktop exposes `/var/run/docker.sock` as `root:root 0660`, hence `DOCKER_GID=0`.
- **node-exporter** binds `/` read-only without `rslave` (not supported in the Docker Desktop VM); metrics are
  of the Docker Desktop VM, not of Windows.
- `scripts/devhub.ps1` sets `DOCKER_HOST` from the active Docker context, `PYTHON`, UTF-8 Python I/O and disables MSYS
  path conversion before calling the bash/python scripts.

## Limits vs the server deployment
- No KVM/libvirt (VM and Windows templates), no public tunnel, no systemd backup timers (run `scripts/backup.sh` by hand
  through `devhub.ps1`).
- Single machine, laptop-sized: keep workspaces at 1-2 vCPU / 1-2 GB.
- Remove everything including data: `devhub.ps1 scripts/dc.sh down -v` (destructive), then delete `secrets\devhub.env`.
