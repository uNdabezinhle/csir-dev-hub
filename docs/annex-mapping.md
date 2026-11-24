# Mapping to the Technical Annex (CSIR/DS/ICSC/SDL/2026/001/E)

Built from the architecture summary supplied by the project. The annex PDF itself was not available to the build.
Status meanings:
- **Implemented:** running and verified on the PoC box.
- **Partial:** code and pipeline exist, but the final step needs hardware this PoC box does not have (stated in the row).
- **Stubbed:** an interface or config exists but has no real backend.
- **Planned:** documented only.

| Annex component | Status | Where / evidence |
|---|---|---|
| Host OS Ubuntu 24.04 / 26.04 LTS | Implemented (workspaces); planned (bare-metal hosts) | `images/ubuntu-server` builds 24.04 and 26.04; host install in docs/extending-kvm.md |
| Portal / orchestrator: self-hosted Coder | Implemented | Coder v2.38.10 in `docker-compose.yml`, :3000 |
| CSIR Dev Hub portal (self-service + admin console) | Implemented | `stack/portal` (FastAPI) on :8088; `scripts/test-portal.py` passes 25/25 |
| Dev environments: Docker + VS Code Server | Implemented | `templates/ubuntu-server` (code-server 4.146, persistent home); verified healthy, HTTP 200 |
| VS Code Desktop (Remote) / JetBrains | Implemented / planned | `display_apps.vscode` through the `ide` parameter; JetBrains in docs/ide-options.md |
| Full desktops: xrdp / noVNC | Implemented | `templates/ubuntu-desktop` (XFCE, xrdp, noVNC app) |
| Brokered RDP (Guacamole), no raw 3389 on LAN | Implemented | `docker-compose.guacamole.yml`, portal `guac.py`; `scripts/test-rdp.py` passes 5/5 (no 3389 published) |
| Session recording | Implemented (basic) | guacd `.guac` recordings in the `guac-recordings` volume; retention planned |
| Specialist OS: Kali (RANGE) | Implemented | `images/kali`, `templates/kali` (isolated profile by default, NET_RAW for nmap/tcpdump); demo-kali verified |
| Windows on KVM/QEMU | Partial: template and pipeline done, not built (nested-KVM kernel BUG on the PoC box) | `templates/windows-kvm` pushed to Coder (q35, OVMF Secure Boot, swtpm TPM 2.0, cloudbase-init, per-workspace RDP credential, brokered RDP through Guacamole). `packer/windows-2022` builds from the official Microsoft **evaluation** ISO and passes `packer validate`. Needs a host with working KVM, see docs/extending-kvm.md |
| KVM capability (VM workspaces) | Implemented, verified under TCG | libvirt container (`docker-compose.kvm.yml`), `scripts/test-kvm.sh` 5/5, `templates/ubuntu-vm` workspace `demo-vm` (agent, ssh, code-server, xrdp through Guacamole), `packer/ubuntu-golden` built and published. Portal Admin → KVM: capacity, domains, power, snapshots. Hardware KVM is off because the box's nested VMX crashes (`KVM_ACCEL=tcg`) |
| Identity: local accounts | Implemented | Portal bcrypt users; Coder password admin (break-glass) |
| Identity: OIDC SSO | Implemented | Keycloak 26.4 realm `csir`; Coder and portal OIDC clients |
| Identity: LDAP / AD federation | Implemented (OpenLDAP); planned (AD) | Keycloak user federation `csir-openldap` plus group mapper; AD is a config change |
| MFA | Implemented | Forced TOTP flow `csir-browser-mfa` plus a role-conditional variant; `scripts/test-mfa.py` passes 5/5 (docs/auth-mfa.md) |
| WebAuthn / passkeys | Planned | docs/auth-mfa.md |
| Roles: hub-admin, image-publisher, project-owner, engineer, auditor, guest | Implemented | `identity/roles.json`, Keycloak realm roles, portal `rbac.py`, Coder role mapping (owner, template-admin, auditor) |
| Quotas (per role, user, project) | Implemented | `policy.json` `role_quotas`, admin → Quotas, projects; denial verified (C3) |
| Idle reclaim / lifecycle | Implemented | `reclaim.py` (idle stop after 120 min; stopped-delete after 14 days, off by default; guest max 7 days), admin → Reclaim |
| FinOps (cost per tier and workspace) | Implemented | admin → FinOps (tiers) and admin → **Billing**. `BillingExporter`: usage per user, project or workspace from Coder build history (running hours), Prometheus/cAdvisor (CPU-hours, average memory) and licence seat-days. Rates in `policy.json` `finops.billing`. CSV or JSON from `/api/billing/usage`. Project owners see only their own projects |
| Network profile: dev | Implemented | Per-workspace bridge network with egress |
| Network profile: isolated (RANGE / Kali) | Implemented | `internal` network plus a socat gateway to the control plane only; no internet egress (verified from demo-kali) |
| Network profile: windows-licensed | Implemented (licence broker), KMS host illustrative | libvirt network `csir-windows-licensed`. `LicenseBroker` seat pools (`windows-kms` 10, `windows-eval` 2): checkout on create, re-checkout on start, return on delete (and on stop for eval), reconcile for workspaces deleted outside the portal, per-role and per-project caps, audit. Shown in admin → Licences and Quotas |
| Persistence: volumes | Implemented | `coder-<wsid>-home` survives template updates and restarts |
| Secrets injection | Implemented | Fernet-encrypted store; read-only `/run/csir-secrets` per user (D1–D3) |
| Observability: metrics / dashboards | Implemented | Prometheus (coder, node, cadvisor, keycloak, portal), Grafana dashboard with 18 panels, alert rules |
| Audit | Implemented | Portal audit log (UI, filter, CSV export), Keycloak events, Coder audit (enterprise) noted |
| Central log aggregation (Loki/ELK) | Planned | docs/extending-kvm.md |
| Image pipeline: publish, scan, approve | Implemented | Trivy 0.76.2 in CI (report-only) and in the portal (`ImageScanner`). The image-publisher role scans and approves. Publishing is blocked on criticals over `max_critical` unless an approved, time-limited risk acceptance exists (hub-admin). Optional gate on workspace creation (`enforce_on_create`) |
| Backup / DR | Implemented, test restore verified | `scripts/backup.sh` (pg_dump for coder, keycloak and guacamole_db; portal SQLite; LDAP slapcat; named volumes; config; encrypted secrets; sha256 manifest; retention 7 daily / 4 weekly). `scripts/restore.sh --verify` restores into scratch DBs and compares row counts. Unit files and a crontab line in docs/backup.md. Status in the portal |
| HA control plane, TLS, wildcard apps | Planned | docs/extending-kvm.md (a public demo tunnel is documented separately in docs/public-access.md when present) |
| CI/CD | Implemented | `.github/workflows/ci.yml` (compose config, terraform fmt/validate, hadolint, portal build) green on GitHub |
| Sizing for ~30/40/60/70 users | Implemented (docs plus FinOps) | docs/sizing.md; default tier 70 |
