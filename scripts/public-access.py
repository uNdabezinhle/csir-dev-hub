#!/usr/bin/env python3
"""Helpers for public (tunnelled) access to CSIR Dev Hub. Never prints secret values.
  redirects : add the public Coder/portal URLs from public.env to the Keycloak 'coder' and 'devhub-portal'
              clients (local HOST_IP/localhost URIs are kept, stale *.trycloudflare.com URIs are dropped).
  rotate    : rotate every internet-facing human credential and write the new values to secrets/devhub.env:
              Coder 'admin' + portal local 'admin' (CODER_ADMIN_PASSWORD), Keycloak master 'admin'
              (KEYCLOAK_ADMIN_PASSWORD), all LDAP demo users (LDAP_DEMO_PASSWORD), Grafana 'admin'
              (GRAFANA_ADMIN_PASSWORD), Guacamole 'guacadmin' (GUACADMIN_PASSWORD)."""
import json, os, secrets, string, subprocess, sys, urllib.parse, urllib.request
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SECRETS = f"{ROOT}/secrets/devhub.env"
os.environ.setdefault("DOCKER_HOST", "tcp://127.0.0.1:2375")

def envfile(p):
    d = {}
    if os.path.exists(p):
        for l in open(p):
            l = l.strip()
            if l and not l.startswith("#") and "=" in l:
                k, v = l.split("=", 1); d[k] = v
    return d
E = envfile(f"{ROOT}/.env"); S = envfile(SECRETS); P = envfile(f"{ROOT}/public.env")
H = E.get("HOST_IP", "127.0.0.1")
KC = f"http://{H}:{E.get('KEYCLOAK_PORT', '8080')}"; CODER = f"http://{H}:{E.get('CODER_PORT', '3000')}"
GUAC = f"http://{H}:{E.get('GUACAMOLE_PORT', '8089')}/guacamole"; REALM = "csir"

def req(method, url, body=None, headers=None, form=False):
    h = dict(headers or {})
    data = None
    if body is not None:
        if form: data = urllib.parse.urlencode(body).encode(); h["Content-Type"] = "application/x-www-form-urlencoded"
        else: data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=h), timeout=30) as r:
        raw = r.read(); return json.loads(raw) if raw.strip() else None

def kc_token(pw=None):
    return req("POST", f"{KC}/realms/master/protocol/openid-connect/token",
               {"grant_type": "password", "client_id": "admin-cli", "username": "admin",
                "password": pw or S["KEYCLOAK_ADMIN_PASSWORD"]}, form=True)["access_token"]

def set_secret(key, value):
    lines = open(SECRETS).read().splitlines(); found = False
    for i, l in enumerate(lines):
        if l.startswith(key + "="): lines[i] = f"{key}={value}"; found = True
    if not found: lines.append(f"{key}={value}")
    tmp = SECRETS + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f: f.write("\n".join(lines) + "\n")
    os.replace(tmp, SECRETS); os.chmod(SECRETS, 0o600); S[key] = value

def gen(n=24):
    a = string.ascii_letters + string.digits
    while True:
        s = "".join(secrets.choice(a) for _ in range(n))
        if any(c.islower() for c in s) and any(c.isupper() for c in s) and any(c.isdigit() for c in s): return s + "!"

# ---------------------------------------------------------------- redirect URIs
def redirects():
    t = kc_token(); auth = {"Authorization": f"Bearer {t}"}
    for cid, key, path in (("coder", "PUBLIC_CODER_URL", "/api/v2/users/oidc/callback"),
                           ("devhub-portal", "PUBLIC_PORTAL_URL", "/*")):
        c = req("GET", f"{KC}/admin/realms/{REALM}/clients?clientId={cid}", headers=auth)
        if not c: print(f"  {cid}: client missing (run scripts/bootstrap-identity.py)"); continue
        c = c[0]
        keep = [u for u in c.get("redirectUris", []) if "trycloudflare.com" not in u]
        origins = [u for u in c.get("webOrigins", []) if "trycloudflare.com" not in u]
        pub = P.get(key)
        if pub: keep.append(pub + path); origins.append(pub)
        c["redirectUris"] = sorted(set(keep)); c["webOrigins"] = sorted(set(origins))
        req("PUT", f"{KC}/admin/realms/{REALM}/clients/{c['id']}", c, headers=auth)
        print(f"  {cid}: {len(c['redirectUris'])} redirect URIs" + (f" (incl. {pub}{path})" if pub else " (local only)"))

# ---------------------------------------------------------------- rotation
def dexec(container, *args, inp=None):
    r = subprocess.run(["docker", "exec", "-i", container, *args], input=inp, capture_output=True, text=True)
    if r.returncode != 0: raise RuntimeError(f"{container}: {args[0]} failed: {r.stderr.strip()[:200]}")
    return r.stdout

def rotate_coder_and_portal():
    new = gen()
    req("PUT", f"{CODER}/api/v2/users/admin/password", {"old_password": S["CODER_ADMIN_PASSWORD"], "password": new},
        headers={"Coder-Session-Token": S["CODER_SESSION_TOKEN"]})
    code = ("import sys,os;sys.path.insert(0,'/srv');from app import db,auth;pw=sys.stdin.read().strip()\n"
            "with db.tx() as c: c.execute(\"UPDATE users SET password_hash=? WHERE username='admin' AND auth_source='local'\",(auth.hash_pw(pw),))\n"
            "print('ok')")
    r = subprocess.run(["docker", "exec", "-i", "-w", "/srv", "csir-devhub-portal-1", "python3", "-c", code], input=new,
                       capture_output=True, text=True)
    set_secret("CODER_ADMIN_PASSWORD", new)  # Coder already changed -> persist before anything else can fail
    print("  Coder admin: rotated")
    remint_coder_token()
    print("  portal admin:", "rotated" if r.returncode == 0 and "ok" in r.stdout else f"FAILED ({r.stderr.strip()[-200:]})")

def remint_coder_token():
    """A Coder password change revokes the admin's API keys -> mint a new long-lived portal service token
    (CODER_SESSION_TOKEN) and re-login the box's coder CLI. Recreate the portal afterwards (tunnel.sh does)."""
    import time
    sess = req("POST", f"{CODER}/api/v2/users/login", {"email": S["CODER_ADMIN_EMAIL"], "password": S["CODER_ADMIN_PASSWORD"]})["session_token"]
    tok = req("POST", f"{CODER}/api/v2/users/me/keys/tokens", {"lifetime": 8760 * 3600 * 10**9, "scope": "all",
              "token_name": f"devhub-portal-{int(time.time())}"}, headers={"Coder-Session-Token": sess})["key"]
    set_secret("CODER_SESSION_TOKEN", tok)
    r = subprocess.run(["coder", "login", CODER, "--token", tok], capture_output=True, text=True)
    print("  Coder portal token: re-minted;", "coder CLI re-logged in" if r.returncode == 0 else "coder CLI login failed")

def rotate_keycloak_admin():
    new = gen(); t = kc_token(); auth = {"Authorization": f"Bearer {t}"}
    u = req("GET", f"{KC}/admin/realms/master/users?username=admin&exact=true", headers=auth)[0]
    req("PUT", f"{KC}/admin/realms/master/users/{u['id']}/reset-password",
        {"type": "password", "value": new, "temporary": False}, headers=auth)
    set_secret("KEYCLOAK_ADMIN_PASSWORD", new); kc_token(new); print("  Keycloak master admin: rotated")

def rotate_ldap_demo():
    roles = json.load(open(f"{ROOT}/identity/roles.json")); new = gen()
    for u in roles["demo_users"]:
        dexec("csir-devhub-openldap-1", "ldappasswd", "-x", "-H", "ldap://localhost", "-D", "cn=admin,dc=csir,dc=local",
              "-w", S["LDAP_ADMIN_PASSWORD"], "-s", new, f"uid={u['uid']},ou=people,dc=csir,dc=local")
    set_secret("LDAP_DEMO_PASSWORD", new); print(f"  LDAP demo users ({len(roles['demo_users'])}): rotated")

def rotate_grafana():
    new = gen()
    dexec("csir-devhub-grafana-1", "grafana", "cli", "--homepath", "/usr/share/grafana", "admin", "reset-admin-password", "--password-from-stdin", inp=new + "\n")
    set_secret("GRAFANA_ADMIN_PASSWORD", new); print("  Grafana admin: rotated")

def rotate_guacadmin():
    new = gen()
    tok = req("POST", f"{GUAC}/api/tokens", {"username": "guacadmin", "password": S["GUACADMIN_PASSWORD"]}, form=True)["authToken"]
    req("PUT", f"{GUAC}/api/session/data/postgresql/users/guacadmin/password?token={tok}",
        {"oldPassword": S["GUACADMIN_PASSWORD"], "newPassword": new})
    set_secret("GUACADMIN_PASSWORD", new); print("  Guacamole guacadmin: rotated")

def rotate():
    for f in (rotate_coder_and_portal, rotate_keycloak_admin, rotate_ldap_demo, rotate_grafana, rotate_guacadmin):
        try: f()
        except Exception as e: print(f"  {f.__name__}: FAILED ({str(e)[:200]})")

if __name__ == "__main__":
    {"redirects": redirects, "rotate": rotate, "remint-coder-token": remint_coder_token}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
