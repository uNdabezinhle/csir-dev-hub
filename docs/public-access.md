# Public access (HTTPS tunnels) — CSIR Dev Hub PoC

Exposes the portal, Coder, Keycloak, Grafana and Guacamole on public HTTPS URLs without opening inbound ports,
using Cloudflare Tunnel. **Local mode stays the default**: everything public lives in an overlay that is only
active while `public.env` exists.

| Piece | Purpose |
|---|---|
| `scripts/tunnel.sh up\|down\|status` | start/stop quick tunnels, write/remove `public.env`, recreate the affected services, fix OIDC redirect URIs |
| `public.env` (git-ignored, runtime) | `PUBLIC_{PORTAL,CODER,KEYCLOAK,GRAFANA,GUACAMOLE}_URL` + public `OIDC_ISSUER_URL` |
| `docker-compose.public.yml` | Coder `CODER_ACCESS_URL` + OIDC issuer, Keycloak `KC_HOSTNAME` (+ backchannel dynamic, x-forwarded headers), portal links/redirects, Grafana `root_url` |
| `scripts/dc.sh` | adds `--env-file public.env -f docker-compose.public.yml` automatically when `public.env` exists |
| `scripts/public-access.py redirects` | adds the public Coder callback / portal `/*` to the Keycloak `coder` / `devhub-portal` clients (local URIs kept, stale tunnel URIs dropped). `bootstrap-identity.py` also keeps them on re-run |
| `scripts/public-access.py rotate` | rotates every human, internet-facing credential (see below) |
| `logs-tunnel/` (git-ignored) | cloudflared logs + pids, `compose.log` |

## Network prerequisite (important)
cloudflared connects **outbound** to the Cloudflare edge on **port 7844** (TCP for `--protocol http2`, UDP for QUIC),
hosts `region{1,2}.v2.argotunnel.com`. If that egress is blocked, cloudflared still prints a `*.trycloudflare.com`
hostname but the URL returns **HTTP 530** because no connector is attached. `tunnel.sh up` checks for
`Registered tunnel connection` and aborts *without changing anything* if it never appears.
Check: `timeout 30 cloudflared tunnel --no-autoupdate --protocol http2 --url http://localhost:8088 2>&1 | grep -E 'Registered|ERR'`.

Install cloudflared (static binary):
```bash
curl -fsSL -o /usr/local/bin/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
chmod +x /usr/local/bin/cloudflared     # or put it in ./tools-bin/ (git-ignored; tunnel.sh adds it to PATH)
```

## Use
```bash
python3 scripts/public-access.py rotate   # once, before going public (writes secrets/devhub.env, chmod 600)
./scripts/tunnel.sh up                    # prints the 5 public URLs (≈2–3 min: tunnels are started one by one)
./scripts/tunnel.sh status
./scripts/tunnel.sh down                  # back to http://HOST_IP:PORT
```
Run these from a clean shell — do **not** `source secrets/devhub.env` into your shell first: exported variables
beat compose `--env-file` values (the overlay pins the issuer explicitly for this reason).

**Quick-tunnel URLs are random and change every time the tunnels restart** (box reboot, `tunnel.sh up`, cloudflared
crash). `tunnel.sh up` re-applies everything (Coder access URL, Keycloak hostname, redirect URIs, portal links) for the new
URLs. trycloudflare also rate-limits bursts (HTTP 429), so the script starts tunnels sequentially with back-off.
Quick tunnels have no uptime guarantee and are for demos only.

## Credentials (no values here — see `secrets/devhub.env`)
| Login | Username | Secret key |
|---|---|---|
| Portal local break-glass hub-admin | `admin` | `CODER_ADMIN_PASSWORD` |
| Coder password login | `admin@csir.local` (`CODER_ADMIN_EMAIL`) | `CODER_ADMIN_PASSWORD` |
| SSO (Keycloak realm `csir`, LDAP users; TOTP enrolment on first login) | `ndabezinhle`, `ipublisher`, `powner`, `engineer1`, `engineer2`, `auditor1`, `guest1` | `LDAP_DEMO_PASSWORD` |
| Keycloak admin console (master realm) | `admin` | `KEYCLOAK_ADMIN_PASSWORD` |
| Grafana | `admin` | `GRAFANA_ADMIN_PASSWORD` |
| Guacamole native UI | `guacadmin` | `GUACADMIN_PASSWORD` |

`rotate` notes: changing the Coder admin password revokes all of admin's API keys, so it re-mints the portal service
token (`CODER_SESSION_TOKEN`) and re-logs-in the box's `coder` CLI — recreate the portal afterwards
(`./scripts/dc.sh up -d portal`; `tunnel.sh up` does it). Sign-up hardening: Coder OSS password sign-up is first-user-only;
new Coder users come only via SSO (Keycloak, MFA enforced) or the portal; Keycloak self-registration is off; Grafana sign-up off.

## What works / doesn't over quick tunnels
* Works (by design): portal (local + SSO login), Coder dashboard, path-based apps incl. **code-server**
  (`subdomain = false` in all templates), Keycloak login + TOTP, Grafana, Guacamole web/RDP via the portal's
  "Open desktop" (Guacamole uses WebSockets, which Cloudflare tunnels carry).
* Not available: Coder **subdomain apps** (no wildcard hostname on trycloudflare — `CODER_WILDCARD_ACCESS_URL` is unset),
  `coder ssh` / VS Code Desktop from outside need the Coder CLI pointed at the public Coder URL (works over HTTPS/DERP-over-WebSocket, slower),
  raw RDP/SSH ports (intentionally never exposed).
* Workspace agents: running workspaces keep their internal `CODER_AGENT_URL` (`http://HOST_IP:3000`).
  Workspaces **built while public** get `access_url` = the tunnel URL, so their agent goes out through the tunnel
  (works for dev-profile templates if containers have HTTPS egress; **breaks the isolated Kali gateway**, whose socat
  forwards to `host:port` of the access URL). Recommended template fix: an `agent_internal_url` template variable
  (default `http://<HOST_IP>:3000`) used for `local.agent_url`/the gateway instead of `data.coder_workspace.me.access_url`.
* Browser and containers must agree on the OIDC issuer: Coder and the portal fetch discovery/JWKS/token from the public
  Keycloak URL (outbound HTTPS through Cloudflare). Scripts that drive login against `http://HOST_IP:*`
  (`scripts/test-*.py`) will be redirected to the public URLs while public mode is on.

## What actually works on the PoC box: Pinggy "lite" mode (account-free, TLS on 443)

The PoC box's egress only passes TLS/HTTP on 443 (a Pingora proxy), so Cloudflare (port 7844) fails and plain
SSH tunnels (localhost.run / Pinggy / serveo on 22 or raw 443) get `kex_exchange_identification: banner line 0:
HTTP/1.1 400 Bad Request` / `Server: Pingora`; localhost.run over TLS gets `tlsv1 alert access denied`.
Pinggy's documented *SSH-over-SSL* mode (ssh with `ProxyCommand=openssl s_client -connect a.pinggy.io:443`) works.

```bash
./scripts/tunnel-pinggy.sh up       # background supervisor + 4 tunnels; prints the URLs (~15 s)
./scripts/tunnel-pinggy.sh status   # LIVE health of each URL (public DNS + HTTP via the public IP)
./scripts/tunnel-pinggy.sh down     # stop + portal back to local URLs
```

| Public | Service | Login over the public URL |
|---|---|---|
| `PUBLIC_PORTAL_URL` | CSIR Dev Hub portal (admin console, provisioning, RDP broker) | `admin` / `CODER_ADMIN_PASSWORD` (local login) |
| `PUBLIC_CODER_URL` | Coder dashboard + browser VS Code (path apps, WebSockets OK) | `admin@csir.local` / `CODER_ADMIN_PASSWORD` (password login) |
| `PUBLIC_GRAFANA_URL` | Grafana | `admin` / `GRAFANA_ADMIN_PASSWORD` |
| `PUBLIC_GUACAMOLE_URL` | Guacamole (target of the portal's "Open desktop" token redirect) | portal-minted token; `guacadmin` / `GUACADMIN_PASSWORD` |

* **Lifetime:** free Pinggy tunnels expire after **60 minutes** and come back with a **new random hostname**.
* **Health checks (every 60 s, `HEALTH_INTERVAL`):** the supervisor (`logs-tunnel/pinggy-supervisor.log`) checks each
  tunnel three ways:
  1. the ssh process is still running;
  2. the hostname resolves through **public DNS** (`https://dns.google/resolve`);
  3. an HTTP request sent **to that public IP** (`curl --resolve`) answers 2xx/3xx.

  The box's own resolver hands out a local proxy address for every hostname, so on its own it can't tell whether a
  tunnel is reachable from outside. That gap is how a dead URL went on being reported as "current" on 2026-11-24.
* **Recovery:** if a check fails (and fails again on a re-check 10 s later), or the ssh process has exited or hung,
  the supervisor kills that tunnel and starts a new one. It waits until the new hostname passes the public checks, then
  rewrites `public.env` and recreates the portal (lite mode only) so its links follow. Every step is logged.
  Measured recovery: a killed tunnel was replaced in about 35 s, and a hung (SIGSTOP) tunnel in about 75 s.
* **Finding the current URLs:** `tunnel-pinggy.sh status`, `logs-tunnel/public-urls.json` (rewritten after every
  check), or **`GET /public-urls`** on the portal. That endpoint needs a signed-in user and returns the URLs,
  their health, the last check time and a `stale` flag. A box restart stops the tunnels; run `up` again.
* **Browser interstitial:** the first visit to each free hostname shows a Pinggy "Caution… served for free" page;
  click through once per hostname (portal, Coder, Guacamole, Grafana).
* **Lite = Coder and Keycloak configs untouched.** `CODER_ACCESS_URL`, the OIDC issuer and `KC_HOSTNAME` stay on
  `HOST_IP`, so hourly URL changes can't cause the issuer mismatch that crash-loops Coder, and workspace agents keep
  their internal URL. Consequence: **SSO/MFA via Keycloak is local-only** (the "Sign in with CSIR SSO" buttons point
  at the private Keycloak); use local/password logins publicly. Coder may show an "access URL" warning banner — harmless.
* For stable hostnames + SSO from outside use the full mode (`tunnel.sh`, named Cloudflare tunnel) from a host whose
  egress allows port 7844, or a paid Pinggy/ngrok reserved domain written into `public.env` with full mode.

### Pinggy account (token) — dashboard visibility
Put the user's Pinggy access token in `secrets/devhub.env` as `PINGGY_TOKEN=` (never on the command line / in git) and
run `./scripts/tunnel-pinggy.sh up`. The script writes a chmod-600 `secrets/pinggy-ssh.conf` (`User <token>[+force]`) and
answers Pinggy's password prompt with an empty password (`scripts/pinggy-askpass.sh`), keeping the TLS-on-443 ProxyCommand.

Verified on the PoC (2026-11-24): the token is accepted (`You are authenticated as <account e-mail>`), the account is
**Free** (`Your tunnel will expire in 60 minutes`), and a Free token allows **one active tunnel at a time**
(a second login gets `Login is not allowed: A tunnel with the same token (…) is already active. Close the existing
tunnel, or use the force option.`). Therefore:
* `PINGGY_ACCOUNT_SERVICES` (default `portal`) — only the portal tunnel runs under the account and shows at
  dashboard.pinggy.io; Coder, Grafana and Guacamole stay anonymous (4 tunnels in total, all health-checked).
* With one account service the login uses `<token>+force`, so a reconnect always replaces a stale session.
  Side effect: it also closes any other tunnel the user starts with the same token elsewhere.
* Free URLs still rotate hourly (and on every reconnect); `/public-urls` and `tunnel-pinggy.sh status` show the
  account per tunnel (`free:<email>` / `anonymous`).
* **Pro**: set `PINGGY_ACCOUNT_SERVICES="portal coder grafana guacamole"` (no `+force`) and use persistent
  subdomains; with stable hostnames the full public mode (Keycloak SSO/MFA over the public URLs) becomes possible.

## Stable URLs: named tunnel (recommended beyond demos / on-prem)

Confirm the tunnel serves the portal over HTTPS and the `devhub_session` cookie has the `Secure` attribute.
Use the configured local portal URL for LAN HTTP testing. If a scripted portal POST returns 403, obtain a CSRF token
using the same cookie jar and portal origin as the POST. See [Portal browser sessions](auth-mfa.md#portal-browser-sessions).
Needs a Cloudflare account + a zone (e.g. `devhub.example.org`), same port-7844 egress.
```bash
cloudflared tunnel login                         # browser auth, writes ~/.cloudflared/cert.pem
cloudflared tunnel create csir-devhub            # writes ~/.cloudflared/<UUID>.json (keep secret)
for h in portal coder auth grafana rdp; do cloudflared tunnel route dns csir-devhub $h.devhub.example.org; done
cat > ~/.cloudflared/config.yml <<'YML'
tunnel: csir-devhub
credentials-file: /root/.cloudflared/<UUID>.json
ingress:
  - {hostname: portal.devhub.example.org,  service: http://localhost:8088}
  - {hostname: coder.devhub.example.org,   service: http://localhost:3000}
  - {hostname: "*.coder.devhub.example.org", service: http://localhost:3000}   # enables Coder subdomain apps
  - {hostname: auth.devhub.example.org,    service: http://localhost:8080}
  - {hostname: grafana.devhub.example.org, service: http://localhost:3001}
  - {hostname: rdp.devhub.example.org,     service: http://localhost:8089}
  - service: http_status:404
YML
cloudflared tunnel run csir-devhub               # or: cloudflared service install
```
Then write `public.env` by hand with those hostnames (plus `OIDC_ISSUER_URL=https://auth.devhub.example.org/realms/csir`),
optionally add `CODER_WILDCARD_ACCESS_URL=*.coder.devhub.example.org` to the overlay, and run
`./scripts/dc.sh up -d keycloak coder portal grafana && python3 scripts/public-access.py redirects`.
Put Cloudflare Access (SSO policy) in front of Keycloak admin / Grafana for production. On the CSIR LAN, prefer a
reverse proxy (Caddy/Traefik) with internal DNS + TLS instead of any tunnel.
