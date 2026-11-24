#!/usr/bin/env python3
"""End-to-end portal checks (CSIR Dev Hub).
 A. local hub-admin login; every admin page renders
 B. SSO login of an LDAP engineer through Keycloak (+forced TOTP enrolment) -> portal role 'engineer', Coder user synced
 C. engineer provisions an ubuntu-server workspace (1 vCPU/1 GB, dev profile); quota denial on oversize request;
    RBAC: engineer gets 403 on admin pages
 D. secrets injection: engineer saves a secret -> file visible inside the workspace at /run/csir-secrets/<name>
Usage: scripts/test-portal.py [--keep]   (deletes the test workspace unless --keep)"""
import sys, os, re, html, json, time, hmac, hashlib, struct, base64, subprocess, urllib.request, urllib.parse, http.cookiejar
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
S = dict(l.strip().split("=", 1) for l in open(f"{ROOT}/secrets/devhub.env") if "=" in l and not l.startswith("#"))
E = dict(l.strip().split("=", 1) for l in open(f"{ROOT}/.env") if "=" in l and not l.startswith("#"))
H = E["HOST_IP"]; P = f"http://{H}:{E['ADMIN_CONSOLE_PORT']}"; KC = f"http://{H}:{E['KEYCLOAK_PORT']}"
keep = "--keep" in sys.argv; results = []
def check(name, ok, extra=""): results.append(ok); print(("PASS " if ok else "FAIL ") + name + (f"  [{extra}]" if extra and not ok else ""))
def session():
    jar = http.cookiejar.CookieJar(); return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
def get(op, url):
    try: r = op.open(url); return r.status, r.read().decode(errors="ignore"), r.geturl()
    except urllib.error.HTTPError as e: return e.code, e.read().decode(errors="ignore"), url
def post(op, url, data):
    try: r = op.open(url, urllib.parse.urlencode(data, doseq=True).encode()); return r.status, r.read().decode(errors="ignore"), r.geturl()
    except urllib.error.HTTPError as e: return e.code, e.read().decode(errors="ignore"), url
def totp(secret):
    sec = secret.replace(" ", "").upper(); key = base64.b32decode(sec + "=" * (-len(sec) % 8))
    h = hmac.new(key, struct.pack(">Q", int(time.time()) // 30), hashlib.sha1).digest(); o = h[-1] & 15
    return "%06d" % ((struct.unpack(">I", h[o:o+4])[0] & 0x7fffffff) % 1000000)
def kc_admin(method, path, body=None):
    t = json.load(urllib.request.urlopen(f"{KC}/realms/master/protocol/openid-connect/token", urllib.parse.urlencode(
        {"grant_type": "password", "client_id": "admin-cli", "username": "admin", "password": S["KEYCLOAK_ADMIN_PASSWORD"]}).encode()))["access_token"]
    r = urllib.request.urlopen(urllib.request.Request(KC + "/admin/realms/csir" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None, headers={"Authorization": "Bearer " + t, "Content-Type": "application/json"}))
    d = r.read(); return json.loads(d) if d else None

# A. local admin
adm = session()
st, body, url = post(adm, f"{P}/login", {"username": "admin", "password": S["CODER_ADMIN_PASSWORD"]})
check("A1 local admin login", st == 200 and "My workspaces" in body and "hub-admin" in body, f"{st} {url}")
for page in ["/admin/users", "/admin/workspaces", "/admin/images", "/admin/quotas", "/admin/reclaim", "/admin/audit", "/admin/health", "/admin/finops", "/projects", "/secrets"]:
    st, body, _ = get(adm, P + page); check(f"A2 admin page {page}", st == 200, str(st))
st, body, _ = get(adm, f"{P}/admin/health"); check("A3 health page: all services up", body.count("badge err") == 0, re.findall(r'<td>([^<]+)</td><td><span class="badge err', body))

# B. SSO engineer (LDAP + forced TOTP)
user = "engineer2"
uid = kc_admin("GET", f"/users?username={user}&exact=true")[0]["id"]
for c in kc_admin("GET", f"/users/{uid}/credentials"):
    if c["type"] == "otp": kc_admin("DELETE", f"/users/{uid}/credentials/{c['id']}")
eng = session()
st, page, url = get(eng, f"{P}/auth/oidc/login")
st, page, url = post(eng, html.unescape(re.search(r'action="([^"]+)"', page).group(1)), {"username": user, "password": S["LDAP_DEMO_PASSWORD"]})
if "kc-totp-secret-key" not in page:
    m = re.search(r'href="([^"]*mode=manual[^"]*)"', page); st, page, url = get(eng, urllib.parse.urljoin(url, html.unescape(m.group(1))))
secret = re.sub(r"<[^>]+>", "", re.search(r'id="kc-totp-secret-key"[^>]*>(.*?)</', page, re.S).group(1)).strip()
st, body, url = post(eng, html.unescape(re.search(r'action="([^"]+)"', page).group(1)), {"totp": totp(secret),
     "totpSecret": html.unescape(re.search(r'name="totpSecret"[^>]*value="([^"]*)"', page).group(1)), "mode": "manual", "userLabel": "portal-e2e"})
check("B1 portal SSO login via Keycloak (LDAP password + TOTP enrolment)", st == 200 and "My workspaces" in body, url)
st, me, _ = get(eng, f"{P}/api/me"); me = json.loads(me) if st == 200 else {}
check("B2 role mapped from LDAP group -> engineer", me.get("roles") == ["engineer"], me.get("roles"))
st, _, _ = get(eng, f"{P}/admin/users"); check("B3 RBAC: engineer denied /admin/users (403)", st == 403, st)
st, _, _ = get(eng, f"{P}/admin/audit"); check("B4 RBAC: engineer denied /admin/audit (403)", st == 403, st)

# C. provisioning + quota
name = "e2e-server"
st, body, _ = post(eng, f"{P}/workspaces", {"template": "ubuntu-server", "name": name, "cpu": 1, "memory": 1, "profile": "dev", "ubuntu_version": "24.04"})
check("C1 engineer provisions ubuntu-server workspace", "being provisioned" in body, re.findall(r'class="flash[^"]*">([^<]+)', body))
st, body, _ = post(eng, f"{P}/workspaces", {"template": "ubuntu-server", "name": "e2e-too-big", "cpu": 4, "memory": 16, "profile": "dev"})
check("C2 quota enforced (16 GB request > engineer quota 12 GB denied, nothing provisioned)", "Quota exceeded" in body, re.findall(r'class="flash[^"]*">([^<]+)', body))
ws = None
for _ in range(60):
    time.sleep(5)
    st, data, _ = get(eng, f"{P}/api/workspaces"); arr = json.loads(data) if st == 200 else []
    ws = next((w for w in arr if w["name"] == name), None)
    if ws and ws["status"] == "running" and ws["agent"] == "connected": break
check("C3 workspace running + agent connected", bool(ws and ws["status"] == "running" and ws["agent"] == "connected"), ws and (ws["status"], ws["agent"]))
if ws:
    # The portal's owner token cannot open another user's path-based app (Coder blocks site owners from user IDEs by
    # design), so verify via the Coder API app healthcheck (Coder agent probes code-server /healthz).
    ok = False
    for _ in range(24):
        r = json.load(urllib.request.urlopen(urllib.request.Request(f"http://{H}:{E['CODER_PORT']}/api/v2/workspaces/{ws['id']}",
                                                                     headers={"Coder-Session-Token": S["CODER_SESSION_TOKEN"]})))
        apps = {a["slug"]: a["health"] for res in r["latest_build"]["resources"] for ag in res.get("agents") or [] for a in ag["apps"]}
        if apps.get("code-server") == "healthy": ok = True; break
        time.sleep(5)
    check("C4 VS Code (code-server) app healthy for the new workspace (Coder app healthcheck)", ok)
# D. secrets
st, body, _ = post(eng, f"{P}/secrets", {"name": "E2E_TOKEN", "value": "e2e-value-123"})
check("D1 secret saved", "saved and injected" in body)
if ws:
    out = subprocess.run(["docker", "exec", f"coder-{user}-{name}", "cat", "/run/csir-secrets/E2E_TOKEN"], capture_output=True, text=True,
                         env={**os.environ, "DOCKER_HOST": os.environ.get("DOCKER_HOST", "tcp://127.0.0.1:2375")})
    check("D2 secret visible read-only inside workspace at /run/csir-secrets/E2E_TOKEN", out.stdout.strip() == "e2e-value-123", out.stderr[:100])
    w = subprocess.run(["docker", "exec", f"coder-{user}-{name}", "sh", "-c", "echo x > /run/csir-secrets/hack"], capture_output=True, text=True,
                       env={**os.environ, "DOCKER_HOST": os.environ.get("DOCKER_HOST", "tcp://127.0.0.1:2375")})
    check("D3 secrets mount is read-only", w.returncode != 0)
post(eng, f"{P}/secrets/delete", {"name": "E2E_TOKEN"})
st, body, _ = post(eng, f"{P}/workspaces", {"template": "ubuntu-server", "name": "e2e-isolated", "cpu": 1, "memory": 1, "profile": "windows-licensed"})
check("C5 network-profile policy: windows-licensed not allowed on ubuntu-server", "not allowed" in body, re.findall(r'class="flash[^"]*">([^<]+)', body))
st, body, _ = get(adm, f"{P}/admin/audit?actor={user}")
check("E1 audit trail recorded login/create/denied/secret events", all(a in body for a in ["login.oidc", "workspace.create", "denied", "secret.put"]))
if ws and not keep:
    post(eng, f"{P}/workspaces/{ws['id']}/delete", {}); print("cleanup: e2e workspace delete requested")
for c in kc_admin("GET", f"/users/{uid}/credentials"):
    if c["type"] == "otp": kc_admin("DELETE", f"/users/{uid}/credentials/{c['id']}")
print(f"{sum(results)}/{len(results)} checks passed"); sys.exit(0 if all(results) else 1)
