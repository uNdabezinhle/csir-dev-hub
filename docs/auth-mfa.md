# Authentication, LDAP federation and MFA - CSIR Dev Hub

## Components
```mermaid
flowchart LR
  U[User browser] -->|1. Sign in with CSIR SSO| C[Coder :3000]
  U -->|or| P[Dev Hub portal :8088]
  C -->|OIDC auth code| K[Keycloak :8080 realm csir]
  P -->|OIDC auth code| K
  K -->|bind / search, READ_ONLY| L[(OpenLDAP dc=csir,dc=local)]
  K -->|2. password via LDAP\n3. TOTP (forced)| U
  K -->|4. id_token + groups claim\nonly after MFA| C
```

* **OpenLDAP** (`osixia/openldap`, not published on the host): `ou=people` (inetOrgPerson), `ou=groups` (groupOfNames).
  In production replace with CSIR Active Directory (vendor `Active Directory`, `sAMAccountName`, LDAPS).
* **Keycloak 26.4**, realm `csir` (all config is scripted and idempotent: `scripts/bootstrap-identity.py`):
  * User Federation `csir-openldap` (READ_ONLY, import, full sync hourly / changed users every 5 min).
  * Group mapper `devhub-groups` -> Keycloak groups `devhub-hub-admins`, `devhub-image-publishers`, `devhub-project-owners`,
    `devhub-engineers`, `devhub-auditors`, `devhub-guests`.
  * Realm roles `hub-admin`, `image-publisher`, `project-owner`, `engineer`, `auditor`, `guest` mapped to those groups, plus
    `mfa-required` (composite member of hub-admin, image-publisher, project-owner).
  * OIDC clients `coder` and `devhub-portal` (confidential, auth-code flow, `groups` claim mapper).
* **Coder**: `docker-compose.oidc.yml` overlay (auto-enabled by `scripts/dc.sh` once `OIDC_ISSUER_URL` is set).
  Password login stays enabled for the local break-glass `admin`. GitHub default login disabled.
* **Portal**: local accounts (bcrypt) + OIDC; roles from the `groups` claim (see `identity/roles.json`).

## How MFA is enforced
1. **OTP policy**: TOTP, HmacSHA1, 6 digits, 30 s period, look-ahead 1 (works with Google Authenticator, FreeOTP,
   Microsoft Authenticator, Ente Auth, Aegis...).
2. **Required action** `CONFIGURE_TOTP`: enabled and **Default Action** -> every newly created/imported user must enrol.
3. **Custom browser flow `csir-browser-mfa`** (bound as the realm *Browser flow*): a copy of the built-in `browser` flow where
   the `Browser - Conditional 2FA` sub-flow is changed from CONDITIONAL to **REQUIRED**, `Condition - user configured` and
   `Condition - credential` are DISABLED, **OTP Form is REQUIRED**, WebAuthn/recovery codes DISABLED. Result: after the LDAP
   password, *every* user must pass OTP; a user with no OTP credential is sent to enrolment first.
   Keycloak only redirects back to the client (Coder/portal) with an authorization code after the whole flow succeeds, so
   **Coder never receives tokens for a password-only login**.
4. **Optional variant `csir-browser-mfa-privileged`** (created, not bound): the 2FA sub-flow is CONDITIONAL on
   `Condition - user role = mfa-required`, so only hub-admin / image-publisher / project-owner must use OTP.
   Switch: `MFA_MODE=privileged python3 scripts/bootstrap-identity.py` (or add `MFA_MODE=privileged` to secrets/devhub.env).
5. Brute-force protection is enabled on the realm.

### Verification (automated) - `scripts/test-mfa.py engineer2`
Result on 2026-11-24 (log: `docs/verification-mfa.log`):
```
PASS 1. password accepted (LDAP) but NO Coder session before MFA; enrolment forced
PASS 2. TOTP enrolled -> Coder session issued
PASS 3a. second login: OTP challenge shown, no session yet
PASS 3b. WRONG OTP rejected -> no Coder session
PASS 4. correct OTP -> Coder session issued
```
The script drives the real Coder -> Keycloak redirect, computes RFC 6238 codes from the enrolled secret, and removes the test
OTP credential afterwards. `scripts/test-sso-login.py <uid>` is a quick password-step check.

## Demo steps
Demo LDAP users (password for all: `LDAP_DEMO_PASSWORD` in `secrets/devhub.env`):
`ndabezinhle` (hub-admin + project-owner), `ipublisher` (image-publisher), `powner` (project-owner), `engineer1`, `engineer2`
(engineer), `auditor1` (auditor), `guest1` (guest).

1. Open `http://172.17.0.1:3000` (Coder) and click **Sign in with CSIR SSO (Keycloak / LDAP)**.
2. Log in as `engineer1` with the demo password (checked against OpenLDAP).
3. Keycloak shows **Mobile Authenticator Setup**: scan the QR code with Google Authenticator / FreeOTP / Ente Auth, enter the
   6-digit code and a device name.
4. You land in Coder as `engineer1` (account auto-created from the OIDC claims).
5. Sign out and in again: password, then **one-time code** prompt. A wrong code is refused.
* Add a new LDAP user: add an `inetOrgPerson` under `ou=people` and to a `devhub-*` group (or edit `identity/roles.json` and
  re-run `scripts/bootstrap-identity.py`), then Keycloak -> User federation -> csir-openldap -> *Sync all users*.
* Add a Keycloak-only user: Keycloak admin console `http://172.17.0.1:8080/admin/master/console/#/csir` (admin password:
  `KEYCLOAK_ADMIN_PASSWORD`), Users -> Add user -> set password; they must enrol TOTP at first login (default action).
* Reset a lost authenticator: Keycloak -> Users -> <user> -> Credentials -> delete `otp`.

## Remaining stubs / next
* WebAuthn / passkeys (FIDO2) as an alternative second factor - planned (add `webauthn-authenticator` ALTERNATIVE to OTP).
* LDAPS/StartTLS and AD-specific mappers - planned for production (PoC uses plain LDAP on an internal Docker network).

## Portal browser sessions

Public HTTPS sessions use cookies marked `Secure`. The explicitly configured LAN HTTP origin uses an HTTP-compatible
cookie for local development. Set `PORTAL_LOCAL_URL` to the actual local portal origin when overriding the Compose
defaults. Portal forms include a CSRF token, including local login and logout. Scripts should fetch `GET /api/csrf`
with their cookie jar, then send the returned token in `X-CSRF-Token` or the `csrf_token` form field with the same
cookie jar. This is separate from the Keycloak OIDC state and nonce checks.
* Coder role/group sync from OIDC claims is a Coder Premium feature; in the OSS PoC the **portal** maps groups to Coder site
  roles via the API (hub-admin -> owner, image-publisher -> template-admin, auditor -> auditor) at portal login.
* Local Coder `admin` / portal `admin` are break-glass accounts without MFA - restrict or remove in production
  (`CODER_DISABLE_PASSWORD_AUTH=true`).
* HTTPS: the PoC runs plain HTTP on the internal host IP; production must front Coder/Keycloak/portal with TLS.
