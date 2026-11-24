#!/usr/bin/env python3
"""Idempotent identity bootstrap for CSIR Dev Hub.
1. Seeds OpenLDAP (ou=people, ou=groups, demo users + role groups from identity/roles.json)
2. Configures Keycloak realm 'csir': TOTP MFA policy, LDAP user federation + group mapper,
   OIDC clients 'coder' and 'devhub-portal' with a 'groups' claim.
3. Writes OIDC_ISSUER_URL into secrets/devhub.env so scripts/dc.sh enables the Coder OIDC overlay.
Secrets are read from secrets/devhub.env and never printed."""
import json, os, subprocess, sys, time, urllib.request, urllib.parse, urllib.error
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
def envfile(p):
    d = {}
    for l in open(p):
        l = l.strip()
        if l and not l.startswith("#") and "=" in l:
            k, v = l.split("=", 1); d[k] = v
    return d
S = envfile(f"{ROOT}/secrets/devhub.env"); E = envfile(f"{ROOT}/.env")
HOST = E["HOST_IP"]; KC = f"http://{HOST}:{E['KEYCLOAK_PORT']}"
REALM = "csir"; BASE_DN = "dc=csir,dc=local"
ROLES = json.load(open(f"{ROOT}/identity/roles.json"))
os.environ.setdefault("DOCKER_HOST", "tcp://127.0.0.1:2375")

def sh(args, inp=None, check=True):
    r = subprocess.run(args, input=inp, capture_output=True, text=True)
    if check and r.returncode != 0 and "Already exists" not in r.stderr and "(68)" not in r.stderr:
        print("ERR", args[:4], r.stderr.strip()[:300]); sys.exit(1)
    return r

# ------------------------------------------------------------------ LDAP
def ldap_container():
    return sh(["docker", "ps", "-qf", "label=com.docker.compose.service=openldap"]).stdout.strip()
def seed_ldap():
    c = ldap_container()
    pw = sh(["docker", "exec", c, "slappasswd", "-s", S["LDAP_DEMO_PASSWORD"]]).stdout.strip()
    ldif = [f"dn: ou=people,{BASE_DN}\nobjectClass: organizationalUnit\nou: people\n",
            f"dn: ou=groups,{BASE_DN}\nobjectClass: organizationalUnit\nou: groups\n"]
    for u in ROLES["demo_users"]:
        given = u["cn"].split()[0]
        ldif.append(f"dn: uid={u['uid']},ou=people,{BASE_DN}\nobjectClass: inetOrgPerson\nuid: {u['uid']}\n"
                    f"cn: {u['cn']}\nsn: {u['sn']}\ngivenName: {given}\nmail: {u['uid']}@csir.local\nuserPassword: {pw}\n")
    for r in ROLES["roles"]:
        members = [u["uid"] for u in ROLES["demo_users"] if r["ldap_group"] in u["groups"]] or ["nobody"]
        ldif.append(f"dn: cn={r['ldap_group']},ou=groups,{BASE_DN}\nobjectClass: groupOfNames\ncn: {r['ldap_group']}\n"
                    f"description: CSIR Dev Hub role {r['id']}\n" +
                    "".join(f"member: uid={m},ou=people,{BASE_DN}\n" for m in members))
    for entry in ldif:  # one by one so re-runs skip existing entries
        r = sh(["docker", "exec", "-i", c, "ldapadd", "-x", "-H", "ldap://localhost", "-D", f"cn=admin,{BASE_DN}",
                "-w", S["LDAP_ADMIN_PASSWORD"]], inp=entry, check=False)
        if r.returncode not in (0, 68): print("ldapadd:", r.stderr.strip()[:200])
    n = sh(["docker", "exec", c, "ldapsearch", "-x", "-H", "ldap://localhost", "-D", f"cn=admin,{BASE_DN}", "-w",
            S["LDAP_ADMIN_PASSWORD"], "-b", f"ou=people,{BASE_DN}", "(objectClass=inetOrgPerson)", "dn"]).stdout.count("dn: uid=")
    print(f"LDAP: {n} people under ou=people")

# ------------------------------------------------------------------ Keycloak REST
TOKEN = None
def kc(method, path, body=None, ok=(200, 201, 204), raw=False):
    req = urllib.request.Request(f"{KC}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            data = r.read(); return (r, data) if raw else (json.loads(data) if data else None)
    except urllib.error.HTTPError as e:
        if e.code == 409: return None
        if e.code in ok: return None
        raise RuntimeError(f"{method} {path}: {e.code} {e.read()[:300]}")
def kc_login():
    global TOKEN
    for _ in range(60):
        try:
            data = urllib.parse.urlencode({"grant_type": "password", "client_id": "admin-cli", "username": "admin",
                                           "password": S["KEYCLOAK_ADMIN_PASSWORD"]}).encode()
            with urllib.request.urlopen(f"{KC}/realms/master/protocol/openid-connect/token", data) as r:
                TOKEN = json.load(r)["access_token"]; return
        except Exception: time.sleep(5)
    sys.exit("Keycloak not reachable")

def client_def(cid, secret, redirects, web_origins):
    return {"clientId": cid, "name": cid, "enabled": True, "protocol": "openid-connect", "publicClient": False,
            "clientAuthenticatorType": "client-secret", "secret": secret, "standardFlowEnabled": True,
            "directAccessGrantsEnabled": False, "redirectUris": redirects, "webOrigins": web_origins,
            "attributes": {"post.logout.redirect.uris": "+"},
            "protocolMappers": [{"name": "groups", "protocol": "openid-connect", "protocolMapper": "oidc-group-membership-mapper",
                                 "config": {"claim.name": "groups", "full.path": "false", "id.token.claim": "true",
                                            "access.token.claim": "true", "userinfo.token.claim": "true"}}]}

def setup_keycloak():
    kc_login()
    if kc("GET", f"/admin/realms/{REALM}", ok=(404,)) is None:
        kc("POST", "/admin/realms", {"realm": REALM, "enabled": True, "displayName": "CSIR Software Development Hub",
            "displayNameHtml": "<b>CSIR</b> Software Development Hub", "loginWithEmailAllowed": True,
            "bruteForceProtected": True, "sslRequired": "none",
            "otpPolicyType": "totp", "otpPolicyAlgorithm": "HmacSHA1", "otpPolicyDigits": 6, "otpPolicyPeriod": 30,
            "otpSupportedApplications": ["totpAppFreeOTPName", "totpAppGoogleName", "totpAppMicrosoftAuthenticatorName"]})
        print("Keycloak: realm created")
    # clients
    for cid, secret, port in (("coder", S["OIDC_CLIENT_SECRET"], E["CODER_PORT"]),
                              ("devhub-portal", S["PORTAL_OIDC_SECRET"], E["ADMIN_CONSOLE_PORT"])):
        base = f"http://{HOST}:{port}"
        redirects = [f"{base}/api/v2/users/oidc/callback"] if cid == "coder" else [f"{base}/*", f"http://localhost:{port}/*"]
        pub = envfile(f"{ROOT}/public.env").get("PUBLIC_CODER_URL" if cid == "coder" else "PUBLIC_PORTAL_URL") \
            if os.path.exists(f"{ROOT}/public.env") else None  # keep tunnel URIs (scripts/tunnel.sh) on re-run
        if pub: redirects.append(f"{pub}/api/v2/users/oidc/callback" if cid == "coder" else f"{pub}/*")
        existing = kc("GET", f"/admin/realms/{REALM}/clients?clientId={cid}")
        d = client_def(cid, secret, redirects, [base] + ([pub] if pub else []))
        if existing:
            cuuid = existing[0]["id"]; d.pop("protocolMappers")
            kc("PUT", f"/admin/realms/{REALM}/clients/{cuuid}", {**existing[0], **d})
        else:
            kc("POST", f"/admin/realms/{REALM}/clients", d)
    print("Keycloak: OIDC clients coder + devhub-portal configured")
    # LDAP federation
    comps = kc("GET", f"/admin/realms/{REALM}/components?type=org.keycloak.storage.UserStorageProvider")
    realm_id = kc("GET", f"/admin/realms/{REALM}")["id"]
    ldap = next((c for c in comps if c["name"] == "csir-openldap"), None)
    if not ldap:
        cfg = {"vendor": ["other"], "connectionUrl": ["ldap://openldap:389"], "bindDn": [f"cn=admin,{BASE_DN}"],
               "bindCredential": [S["LDAP_ADMIN_PASSWORD"]], "usersDn": [f"ou=people,{BASE_DN}"],
               "usernameLDAPAttribute": ["uid"], "rdnLDAPAttribute": ["uid"], "uuidLDAPAttribute": ["entryUUID"],
               "userObjectClasses": ["inetOrgPerson"], "editMode": ["READ_ONLY"], "importEnabled": ["true"],
               "syncRegistrations": ["false"], "searchScope": ["1"], "authType": ["simple"], "pagination": ["false"],
               "trustEmail": ["true"], "fullSyncPeriod": ["3600"], "changedSyncPeriod": ["300"], "enabled": ["true"],
               "priority": ["0"], "cachePolicy": ["DEFAULT"]}
        kc("POST", f"/admin/realms/{REALM}/components", {"name": "csir-openldap", "providerId": "ldap",
            "providerType": "org.keycloak.storage.UserStorageProvider", "parentId": realm_id, "config": cfg})
        comps = kc("GET", f"/admin/realms/{REALM}/components?type=org.keycloak.storage.UserStorageProvider")
        ldap = next(c for c in comps if c["name"] == "csir-openldap")
        print("Keycloak: LDAP federation added")
    mappers = kc("GET", f"/admin/realms/{REALM}/components?parent={ldap['id']}")
    gm = next((m for m in mappers if m["name"] == "devhub-groups"), None)
    if not gm:
        kc("POST", f"/admin/realms/{REALM}/components", {"name": "devhub-groups", "providerId": "group-ldap-mapper",
            "providerType": "org.keycloak.storage.ldap.mappers.LDAPStorageMapper", "parentId": ldap["id"],
            "config": {"groups.dn": [f"ou=groups,{BASE_DN}"], "group.name.ldap.attribute": ["cn"],
                       "group.object.classes": ["groupOfNames"], "preserve.group.inheritance": ["false"],
                       "membership.ldap.attribute": ["member"], "membership.attribute.type": ["DN"],
                       "membership.user.ldap.attribute": ["uid"], "mode": ["READ_ONLY"],
                       "user.roles.retrieve.strategy": ["LOAD_GROUPS_BY_MEMBER_ATTRIBUTE"],
                       "drop.non.existing.groups.during.sync": ["false"], "ignore.missing.groups": ["false"]}})
        mappers = kc("GET", f"/admin/realms/{REALM}/components?parent={ldap['id']}")
        gm = next(m for m in mappers if m["name"] == "devhub-groups")
    kc("POST", f"/admin/realms/{REALM}/user-storage/{ldap['id']}/mappers/{gm['id']}/sync?direction=fedToKeycloak")
    res = kc("POST", f"/admin/realms/{REALM}/user-storage/{ldap['id']}/sync?action=triggerFullSync")
    print("Keycloak: LDAP sync", res)
    # MFA: users flagged mfa_required must enrol TOTP at next login (conditional OTP in browser flow enforces it after)
    for u in ROLES["demo_users"]:
        if u.get("mfa_required"):
            found = kc("GET", f"/admin/realms/{REALM}/users?username={u['uid']}&exact=true")
            if found:
                usr = found[0]; ra = set(usr.get("requiredActions", []))
                creds = kc("GET", f"/admin/realms/{REALM}/users/{usr['id']}/credentials")
                if not any(c["type"] == "otp" for c in creds) and "CONFIGURE_TOTP" not in ra:
                    kc("PUT", f"/admin/realms/{REALM}/users/{usr['id']}", {"requiredActions": sorted(ra | {"CONFIGURE_TOTP"})})
                print(f"Keycloak: MFA (TOTP) enforced for {u['uid']}")
    groups = [g["name"] for g in kc("GET", f"/admin/realms/{REALM}/groups")]
    print("Keycloak: groups", groups)


# ------------------------------------------------------------------ MFA (TOTP) + realm roles
PRIVILEGED = ["hub-admin", "image-publisher", "project-owner"]
def setup_roles():
    """Realm roles = the six portal roles (+ 'mfa-required'), granted via the LDAP-federated groups."""
    kc("POST", f"/admin/realms/{REALM}/roles", {"name": "mfa-required", "description": "Users holding this role must pass TOTP"})
    mfa_role = kc("GET", f"/admin/realms/{REALM}/roles/mfa-required")
    groups = {g["name"]: g["id"] for g in kc("GET", f"/admin/realms/{REALM}/groups")}
    for r in ROLES["roles"]:
        kc("POST", f"/admin/realms/{REALM}/roles", {"name": r["id"], "description": r["description"]})
        role = kc("GET", f"/admin/realms/{REALM}/roles/{r['id']}")
        if r["id"] in PRIVILEGED:
            kc("POST", f"/admin/realms/{REALM}/roles-by-id/{role['id']}/composites", [mfa_role])
        gid = groups.get(r["ldap_group"])
        if gid:
            kc("POST", f"/admin/realms/{REALM}/groups/{gid}/role-mappings/realm", [role])
    print("Keycloak: realm roles", [r["id"] for r in ROLES["roles"]], "+ mfa-required (composite of", PRIVILEGED, ") mapped to LDAP groups")

def _execs(alias):
    return kc("GET", f"/admin/realms/{REALM}/authentication/flows/{urllib.parse.quote(alias, safe='')}/executions")
def _set_req(alias, ex, req):
    if ex["requirement"] != req:
        ex = dict(ex); ex["requirement"] = req
        kc("PUT", f"/admin/realms/{REALM}/authentication/flows/{urllib.parse.quote(alias, safe='')}/executions", ex)

def build_flow(name, mode):
    """mode='all': OTP Form REQUIRED for everyone. mode='privileged': OTP only for users with role mfa-required."""
    # rebuilt from the built-in 'browser' flow on every run => idempotent and drift-free
    flows = {f["alias"]: f for f in kc("GET", f"/admin/realms/{REALM}/authentication/flows")}
    if name in flows:
        kc("DELETE", f"/admin/realms/{REALM}/authentication/flows/{flows[name]['id']}")
    kc("POST", f"/admin/realms/{REALM}/authentication/flows/browser/copy", {"newName": name})
    ex = _execs(name)
    cond = next(e for e in ex if e.get("authenticationFlow") and
                (e["displayName"].endswith("Conditional 2FA") or e["displayName"].endswith("Conditional OTP")))
    sub_alias = cond["displayName"]
    if mode == "all":
        _set_req(name, cond, "REQUIRED")
    else:
        _set_req(name, cond, "CONDITIONAL")
    sub = _execs(sub_alias)
    if mode == "privileged" and not any(e.get("providerId") == "conditional-user-role" for e in sub):
        kc("POST", f"/admin/realms/{REALM}/authentication/flows/{urllib.parse.quote(sub_alias, safe='')}/executions/execution",
           {"provider": "conditional-user-role"})
        sub = _execs(sub_alias)
        ce = next(e for e in sub if e.get("providerId") == "conditional-user-role")
        kc("POST", f"/admin/realms/{REALM}/authentication/executions/{ce['id']}/config",
           {"alias": f"{name}-role", "config": {"condUserRole": "mfa-required", "negate": "false"}})
        for _ in range(len(sub)):
            kc("POST", f"/admin/realms/{REALM}/authentication/executions/{ce['id']}/raise-priority")
        sub = _execs(sub_alias)
    for e in sub:
        pid = e.get("providerId")
        if pid in ("conditional-user-configured", "conditional-credential"): _set_req(sub_alias, e, "DISABLED")
        elif pid == "conditional-user-role": _set_req(sub_alias, e, "REQUIRED")
        elif pid == "auth-otp-form": _set_req(sub_alias, e, "REQUIRED")
        elif pid in ("webauthn-authenticator", "auth-recovery-authn-code-form"): _set_req(sub_alias, e, "DISABLED")
    return [(e["displayName"], e["requirement"]) for e in _execs(name)]

def setup_mfa():
    # 1. OTP policy: TOTP / HmacSHA1 / 6 digits / 30 s (also set at realm creation; re-applied for idempotency)
    kc("PUT", f"/admin/realms/{REALM}", {"otpPolicyType": "totp", "otpPolicyAlgorithm": "HmacSHA1", "otpPolicyDigits": 6,
        "otpPolicyPeriod": 30, "otpPolicyLookAheadWindow": 1,
        "otpSupportedApplications": ["totpAppFreeOTPName", "totpAppGoogleName", "totpAppMicrosoftAuthenticatorName"]})
    # 2. Required action "Configure OTP": enabled + Default Action (new users enrol on first login)
    ra = kc("GET", f"/admin/realms/{REALM}/authentication/required-actions/CONFIGURE_TOTP")
    ra.update({"enabled": True, "defaultAction": True})
    kc("PUT", f"/admin/realms/{REALM}/authentication/required-actions/CONFIGURE_TOTP", ra)
    # 3. Custom browser flows; bind the one selected by MFA_MODE (default: all users)
    mode = os.environ.get("MFA_MODE", S.get("MFA_MODE", "all"))
    kc("PUT", f"/admin/realms/{REALM}", {"browserFlow": "browser"})  # unbind before rebuilding
    f_all = build_flow("csir-browser-mfa", "all")
    f_priv = build_flow("csir-browser-mfa-privileged", "privileged")
    bound = "csir-browser-mfa" if mode == "all" else "csir-browser-mfa-privileged"
    kc("PUT", f"/admin/realms/{REALM}", {"browserFlow": bound})
    print("Keycloak: OTP policy TOTP/HmacSHA1/6/30s; CONFIGURE_TOTP enabled+default")
    print("Keycloak: flow csir-browser-mfa:", f_all)
    print("Keycloak: flow csir-browser-mfa-privileged:", f_priv)
    print("Keycloak: realm browser flow bound to", bound)

def enable_coder_oidc():
    issuer = f"{KC}/realms/{REALM}"
    p = f"{ROOT}/secrets/devhub.env"; lines = open(p).read().splitlines()
    lines = [f"OIDC_ISSUER_URL={issuer}" if l.startswith("OIDC_ISSUER_URL=") else l for l in lines]
    open(p, "w", newline="\n").write("\n".join(lines) + "\n"); os.chmod(p, 0o600)
    print("Coder: OIDC issuer set ->", issuer, "(run ./scripts/dc.sh up -d coder)")

if __name__ == "__main__":
    seed_ldap(); setup_keycloak(); setup_roles(); setup_mfa(); enable_coder_oidc()
