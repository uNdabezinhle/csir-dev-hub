# Brokered RDP with Apache Guacamole

Annex rule: **no raw RDP (3389) on the lab LAN.** Desktop and Windows workspaces are reached only via Guacamole
(HTML5 over HTTP(S)) or Coder's noVNC app. The portal brokers every RDP session.

## Components

| Component | Image | Exposure | Notes |
|---|---|---|---|
| `guacd` | `guacamole/guacd:1.6.0` | **no published ports** (4822 on `csir-devhub-core` only) | proxy daemon that speaks RDP/VNC/SSH |
| `guacamole` | `guacamole/guacamole:1.6.0` | `http://HOST:8089/guacamole/` | web client, Postgres auth plus JSON auth |
| `guacamole_db` | shared `postgres` | internal | role `guacamole`, schema `stack/guacamole/initdb.sql` |
| `guac-recordings` | volume | internal | session recordings (`*.guac`) |

Overlay: `docker-compose.guacamole.yml`. `scripts/dc.sh` adds it when `.env` has `GUACAMOLE_ENABLED=true`.

## Start and verify

```bash
cd /workspace/csir-devhub
./scripts/bootstrap-guacamole.sh   # idempotent: DB/role, schema, start guacd+guacamole, recordings perms, rotate guacadmin
# or just (re)start the containers:
./scripts/dc.sh up -d guacd guacamole
python3 scripts/test-rdp.py demo-desktop   # end-to-end RDP check: 5 checks
```

The guacadmin password is `GUACADMIN_PASSWORD` in `secrets/devhub.env`. The default `guacadmin/guacadmin` is rotated,
and the bootstrap verifies that the default is rejected.

## How a session is brokered (portal-driven, dynamic)

1. The user clicks **Desktop (RDP)** on a running `ubuntu-desktop` workspace in the portal (`/workspaces/{id}/rdp`).
2. The portal checks RBAC: the user must own the workspace or be hub-admin.
3. The portal asks the engine broker over its shared Unix socket to attach `guacd` to the workspace's private bridge network `csir-ws-<owner>-<ws>`.
   This is idempotent. guacd is the only extra member of that network.
4. The portal mints a **guacamole-auth-json** token (HMAC-SHA256 signed, then AES-128-CBC encrypted with
   `GUAC_JSON_SECRET`). It expires after 5 minutes and holds one connection:
   - `protocol=rdp`, `hostname=coder-<owner>-<ws>` (container DNS on the private network), `port=3389`
   - `username=coder`, `password=<per-user RDP password from /run/csir-secrets/rdp_password>`
   - `security=any`, `ignore-cert=true` (xrdp self-signed cert; use `nla` plus a real cert for Windows/AD)
   - `resize-method=display-update`, `timezone=Africa/Johannesburg`
   - `recording-path=/var/lib/guacamole/recordings`, `create-recording-path=true`,
     `recording-name=<user>-<owner>-<ws>-<UTC timestamp>.guac` (turn off with `GUAC_RECORDING=false`)
5. The browser opens `http://HOST:8089/guacamole/?data=<token>`. The user never sees the RDP credential.
6. The portal writes an audit event `workspace.rdp`.

The workspace container publishes nothing on the host. `test-rdp.py` check 5 asserts that no container publishes 3389.

### Windows (KVM) connections
The bundled `windows-kvm` template uses the VM forwarding path below with NLA. Direct routing and enterprise
certificate and directory integration are separate deployment options.

## Recording and playback
- Files are written by guacd (uid 1000) into the `guac-recordings` volume. `bootstrap-guacamole.sh` chowns the volume.
- `RECORDING_SEARCH_PATH` lets admins replay recordings in the Guacamole UI: Settings → History → Play.
  This applies to connections defined in the DB. Recordings from JSON-auth sessions sit in the same directory and
  can be converted with `guacenc` (to video) or kept as audit evidence.
- Recording retention has no automated policy. Before production, assign a data owner and agree retention, access,
  backup handling and deletion evidence. Track acceptance in the [upgrade runbook](upgrade-acceptance.md#production-readiness).

## Identity integration (MFA)
- **Today:** portal users are already authenticated by Keycloak (LDAP plus forced TOTP) before the portal mints a
  token, so RDP inherits MFA. guacadmin is a local break-glass account with a generated password.
- `TOTP_ENABLED=true` (guacamole-auth-totp) is **left off**. It would add a second TOTP prompt for JSON-auth users,
  and it enrols secrets per Guacamole user, which duplicates Keycloak.
- **Recommended next:** `OPENID_AUTHORIZATION_ENDPOINT`, `OPENID_JWKS_ENDPOINT`, `OPENID_ISSUER`,
  `OPENID_CLIENT_ID=guacamole` and `OPENID_REDIRECT_URI` against realm `csir`. Then the direct Guacamole UI uses the
  same SSO and MFA. Map the Keycloak `groups` claim to Guacamole groups (`OPENID_GROUPS_CLAIM_TYPE=groups`).
- LDAP alternative: `LDAP_HOSTNAME=openldap`, `LDAP_USER_BASE_DN=ou=people,dc=csir,dc=local`,
  `LDAP_GROUP_BASE_DN=ou=groups,dc=csir,dc=local`. That gives no MFA unless it is combined with TOTP or Duo.

## Alternative: static connections through the REST API
For long-lived connections (shared lab machines), the portal or an admin can create DB connections:
```
POST /guacamole/api/tokens                                    (guacadmin)
POST /guacamole/api/session/data/postgresql/connections       {"name":..,"protocol":"rdp","parameters":{...}}
POST /guacamole/api/session/data/postgresql/users/{u}/permissions   (grant READ)
```
The JSON-auth approach is preferred: it creates no standing credentials and expires in 5 minutes.

## VM workspaces (libvirt)
VMs live on libvirt networks inside the libvirt host (the `csir-devhub-libvirt-1` container on the PoC). guacd cannot
join those networks, so the portal asks the engine broker to set up the path:
- It adds an iptables DNAT `<libvirt-host>:<20000+hash> -> <vm-ip>:3389` on the libvirt host, accepted **only** from
  guacd's address, plus a matching FORWARD rule.
- It hands Guacamole that host and port, together with the workspace's RDP credential (`rdp_password`, a sensitive
  Coder metadata item generated per workspace by the template).

Port 3389 is still not published anywhere. Proof: `python3 scripts/test-rdp.py demo-vm`. Windows VMs (`windows-kvm`)
use the same path with `security=nla`.
