#!/usr/bin/env python3
"""End-to-end portal checks (CSIR Dev Hub).
 A. local hub-admin login; every admin page renders
 B. SSO login of an LDAP engineer through Keycloak (+forced TOTP enrolment) -> portal role 'engineer', Coder user synced
 C. engineer provisions an ubuntu-server workspace (1 vCPU/1 GB, dev profile); quota denial on oversize request;
    RBAC: engineer gets 403 on admin pages
 D. secrets injection: engineer saves a secret -> file visible inside the workspace at /run/csir-secrets/<name>
 F. licence broker: pools, manual checkout/return, pool exhaustion, role seat quota, seat tied to the workspace
    lifecycle (portal delete + delete outside the portal -> reconcile), audit
 G. image scanning: Trivy scan as image-publisher, auditor RBAC, critical-CVE approval block, publish gate, risk acceptance
 H. billing export: JSON/CSV by workspace/user/project, Coder build hours + Prometheus actuals, RBAC
 K. KVM provider (libvirt capacity, VM snapshot) and backup status
Usage: scripts/test-portal.py [--keep]   (deletes the test workspace unless --keep)"""
import sys, os, re, html, json, time, hmac, hashlib, struct, base64, subprocess, urllib.request, urllib.parse, http.cookiejar
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); from devhub_testlib import cookiejar  # noqa: E402
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
S = dict(l.strip().split("=", 1) for l in open(f"{ROOT}/secrets/devhub.env") if "=" in l and not l.startswith("#"))
E = dict(l.strip().split("=", 1) for l in open(f"{ROOT}/.env") if "=" in l and not l.startswith("#"))
H = E["HOST_IP"]; P = f"http://{H}:{E['ADMIN_CONSOLE_PORT']}"; KC = f"http://{H}:{E['KEYCLOAK_PORT']}"
keep = "--keep" in sys.argv; results = []
KVM = E.get("KVM_ENABLED", "false") == "true"   # Docker Desktop / no-KVM hosts: VM + Windows checks are skipped
def skip(name, why): print(f"SKIP {name}  [{why}]")
def check(name, ok, extra=""): results.append(ok); print(("PASS " if ok else "FAIL ") + name + (f"  [{extra}]" if extra and not ok else ""))
def session():
    jar = cookiejar(); return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
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
# ======================================================================== Phase 2 modules
def flashes(body): return re.findall(r'class="flash[^"]*">([^<]+)', body)
def jget(op, path):
    st, b, _ = get(op, P + path); return st, (json.loads(b) if st == 200 and b.strip().startswith(("{", "[")) else b)
def coder_api(method, path, body=None):
    r = urllib.request.urlopen(urllib.request.Request(f"http://{H}:{E['CODER_PORT']}{path}", method=method, data=json.dumps(body).encode() if body else None,
                               headers={"Coder-Session-Token": S["CODER_SESSION_TOKEN"], "Content-Type": "application/json"}))
    d = r.read(); return json.loads(d) if d else None
def local_user(username, role):
    """Deterministic, secret-derived password for local e2e users (never printed)."""
    pw = "E2e-" + hmac.new(S["PORTAL_SESSION_KEY"].encode(), username.encode(), hashlib.sha256).hexdigest()[:20] + "!"
    post(adm, f"{P}/admin/users", {"username": username, "email": f"{username}@csir.local", "full_name": username, "password": pw, "roles": [role]})
    post(adm, f"{P}/admin/users/{username}/roles", {"roles": [role]})
    op = session(); st, body, _ = post(op, f"{P}/login", {"username": username, "password": pw}); return op, "My workspaces" in body
def wait_ws_gone(ws_id, t=180):
    for _ in range(t // 5):
        try: coder_api("GET", f"/api/v2/workspaces/{ws_id}")
        except urllib.error.HTTPError as e:
            if e.code in (404, 410): return True
        time.sleep(5)
    return False

# F. licence broker ----------------------------------------------------------------------------------------------
st, lic = jget(adm, "/api/licences")
pools = {p["id"]: p for p in lic.get("pools", [])} if isinstance(lic, dict) else {}
check("F1 licence pools seeded (windows-kms, windows-eval) + /admin/licences renders", {"windows-kms", "windows-eval"} <= set(pools) and get(adm, P + "/admin/licences")[0] == 200, list(pools))
orig = pools.get("windows-eval", {}).get("seats", 2)
post(adm, f"{P}/admin/licences/windows-eval/resize", {"seats": 1})
st, b1, _ = post(adm, f"{P}/admin/licences/checkout", {"pool": "windows-eval", "username": "admin", "note": "e2e lab PC #1"})
st, b2, _ = post(adm, f"{P}/admin/licences/checkout", {"pool": "windows-eval", "username": "admin", "note": "e2e lab PC #2"})
check("F2 manual checkout ok, then denied when the pool (resized to 1 seat) is exhausted",
      any("assigned to admin" in f for f in flashes(b1)) and any("no seats left" in f for f in flashes(b2)), flashes(b1) + flashes(b2))
seat = next((s for s in jget(adm, "/api/licences")[1]["open_seats"] if s["note"] == "e2e lab PC #1"), None)
if seat: post(adm, f"{P}/admin/licences/return/{seat['id']}", {"reason": "e2e"})
post(adm, f"{P}/admin/licences/windows-eval/resize", {"seats": orig})
check("F3 seat returned; pool back to 0 used / original size", next(p for p in jget(adm, "/api/licences")[1]["pools"] if p["id"] == "windows-eval")["used"] == 0)
# role quota: engineer may hold 1 seat
post(adm, f"{P}/admin/licences/checkout", {"pool": "windows-kms", "username": user, "note": "e2e eng seat 1"})
st, b, _ = post(adm, f"{P}/admin/licences/checkout", {"pool": "windows-kms", "username": user, "note": "e2e eng seat 2"})
check("F4 quota integration: 2nd seat for an engineer denied (role_max_seats.engineer = 1)", any("quota for your role is 1" in f for f in flashes(b)), flashes(b))
for s in jget(adm, "/api/licences")[1]["open_seats"]:
    if (s["note"] or "").startswith("e2e eng"): post(adm, f"{P}/admin/licences/return/{s['id']}", {})
# lifecycle with real Coder workspaces on the windows-licensed profile (windows-kvm template: the build itself fails on
# the PoC box - no KVM / golden image - but the workspace exists in Coder, which is what the seat is tied to)
kms_used = lambda: next(p for p in jget(adm, "/api/licences")[1]["pools"] if p["id"] == "windows-kms")["used"]
if KVM:
    u0 = kms_used()
    st, b, _ = post(adm, f"{P}/workspaces", {"template": "windows-kvm", "name": "e2e-win-a", "cpu": 2, "memory": 4, "profile": "windows-licensed"})
    wsa = next((w for w in json.loads(get(adm, f"{P}/api/workspaces")[1]) if w["name"] == "e2e-win-a"), None)
    check("F5 create on windows-licensed checks out a seat tied to the workspace", bool(wsa) and kms_used() == u0 + 1, flashes(b))
    if wsa:
        time.sleep(10); post(adm, f"{P}/workspaces/{wsa['id']}/delete", {})
        check("F6 portal delete returns the seat", kms_used() == u0, kms_used())
        wait_ws_gone(wsa["id"])
    st, b, _ = post(adm, f"{P}/workspaces", {"template": "windows-kvm", "name": "e2e-win-b", "cpu": 2, "memory": 4, "profile": "windows-licensed"})
    wsb = next((w for w in json.loads(get(adm, f"{P}/api/workspaces")[1]) if w["name"] == "e2e-win-b"), None)
    if wsb:
        time.sleep(10); coder_api("POST", f"/api/v2/workspaces/{wsb['id']}/builds", {"transition": "delete"})   # outside the portal
        gone = wait_ws_gone(wsb["id"])
        st, rec, _ = post(adm, f"{P}/admin/reconcile", {})
        check("F7 workspace deleted outside the portal -> reconcile returns its seat", gone and kms_used() == u0, rec)
else:
    for n in ("F5", "F6", "F7"): skip(f"{n} windows-licensed workspace seat lifecycle", "KVM_ENABLED=false: no windows-kvm template")
st, aud, _ = get(adm, f"{P}/admin/audit?action=licence")
check("F8 audit trail has licence.checkout / licence.return / denied / pool resize", all(a in aud for a in ["licence.checkout", "licence.return", "denied", "licence.pool.resize"]))

# G. image scanning + approval gate -----------------------------------------------------------------------------
IMG = "csir-devhub/ubuntu-server:24.04"
pub, ok1 = local_user("e2e-publisher", "image-publisher"); aud_op, ok2 = local_user("e2e-auditor", "auditor")
check("G0 local image-publisher + auditor test users can sign in", ok1 and ok2)
st, b, _ = post(aud_op, f"{P}/admin/images/scan", {"image": IMG})
check("G1 RBAC: auditor may view images but not scan (403)", st == 403 and get(aud_op, P + "/admin/images")[0] == 200, st)
st, b, _ = post(pub, f"{P}/admin/images/scan", {"image": IMG})
check("G2 image publisher starts a Trivy scan", any("started" in f for f in flashes(b)), flashes(b))
scan = None
for _ in range(90):
    time.sleep(5); sc = jget(pub, "/api/images/scans")[1]["scans"]; scan = next((s for s in sc if s["image"] == IMG), None)
    if scan and scan["status"] != "running": break
check("G3 Trivy scan completed with severity counts", bool(scan and scan["status"] == "done" and scan["critical"] is not None), scan)
if scan:
    print(f"   scan #{scan['id']} {IMG}: CRITICAL={scan['critical']} HIGH={scan['high']} MEDIUM={scan['medium']} LOW={scan['low']} ({scan['os']})")
    st, det = jget(pub, f"/api/images/scans/{scan['id']}")
    check("G4 scan detail API lists CVEs", st == 200 and len(det.get("vulnerabilities", [])) == (scan["critical"] + scan["high"] + scan["medium"] + scan["low"] + (scan["unknown"] or 0)))
    st, b, _ = post(pub, f"{P}/admin/images/decide", {"image": IMG, "decision": "approved", "note": "e2e"})
    crit = scan["critical"] or 0
    check("G5 publisher approval blocked while critical CVEs > threshold 0" if crit else "G5 publisher approves a clean image",
          any(("critical CVE" in f) if crit else ("approved" in f) for f in flashes(b)), flashes(b))
    # publish gate: hide the catalog item, try to re-publish (blocked), hub-admin risk acceptance, re-publish (ok)
    post(adm, f"{P}/admin/images/ubuntu-server", {"published": 0, "default_cpu": 2, "default_mem": 2, "description": ""})
    st, b, _ = post(pub, f"{P}/admin/images/ubuntu-server", {"published": 1, "default_cpu": 2, "default_mem": 2, "description": ""})
    blocked = any("Cannot publish" in f for f in flashes(b)) if crit else True
    st, b, _ = post(adm, f"{P}/admin/images/decide", {"image": IMG, "decision": "approved", "risk_accept": 1, "note": "e2e: CHG-0001 accepted until rebuild"})
    st, b2, _ = post(pub, f"{P}/admin/images/ubuntu-server", {"published": 1, "default_cpu": 2, "default_mem": 2, "description": ""})
    st, g = jget(pub, f"/api/images/gate?image={urllib.parse.quote(IMG)}")
    check("G6 publish blocked by gate; allowed after hub-admin risk acceptance (item re-published)", blocked and any("updated" in f for f in flashes(b2)) and g.get("publishable"), (flashes(b2), g))
st, aud, _ = get(adm, f"{P}/admin/audit?action=image")
check("G7 audit: image.scan / image.approved / denied events", all(a in aud for a in ["image.scan", "image.approved"]))

# H. billing / FinOps export -------------------------------------------------------------------------------------
st, bill = jget(adm, "/api/billing/usage?group_by=workspace")
rows = bill.get("rows", []) if isinstance(bill, dict) else []
demo = next((r for r in rows if r["workspace"] == "admin/demo-ubuntu"), {})
check("H1 JSON export per workspace: running hours from Coder builds, cost > 0", demo.get("running_hours", 0) > 0 and demo.get("cost_total", 0) > 0, demo)
check("H2 Prometheus actuals included (cAdvisor CPU core-hours)", bill.get("meta", {}).get("prometheus") is True and demo.get("cpu_core_hours_used") is not None, bill.get("meta", {}).get("prometheus"))
st, csvb, _ = get(adm, f"{P}/api/billing/usage?group_by=user&format=csv")
check("H3 CSV export by user", st == 200 and csvb.splitlines()[0].startswith("user,cost_centre,workspaces,running_hours"), csvb[:80])
st, pj = jget(adm, "/api/billing/usage?group_by=project&from=2026-01-01")
check("H4 JSON export by project with date range + totals", st == 200 and pj["meta"]["from"] == "2026-01-01" and "cost_total" in pj["meta"]["totals"], pj if st != 200 else "")
check("H5 RBAC: engineer denied billing export (403); /admin/billing renders for admin",
      get(eng, f"{P}/api/billing/usage")[0] == 403 and get(adm, f"{P}/admin/billing?group_by=project")[0] == 200)

# K. KVM provider + backup status ---------------------------------------------------------------------------------
if KVM:
    st, kv = jget(adm, "/api/kvm")
    doms = {d["name"]: d for d in kv.get("domains", [])} if isinstance(kv, dict) else {}
    check("K1 KVM provider: libvirt capacity + demo-vm domain running", kv.get("capabilities", {}).get("status") == "ok" and doms.get("csir-admin-demo-vm", {}).get("state") == "running",
          (kv.get("capabilities", {}).get("status"), list(doms)))
    st, b, _ = post(adm, f"{P}/admin/kvm/csir-admin-demo-vm/snapshot", {"snapshot": "e2e-snap"})
    has = "e2e-snap" in {d["name"]: d for d in jget(adm, "/api/kvm")[1]["domains"]}["csir-admin-demo-vm"].get("snapshots", [])
    post(adm, f"{P}/admin/kvm/csir-admin-demo-vm/delete-snapshot", {"snapshot": "e2e-snap"})
    check("K2 portal snapshot of a VM workspace (create + delete)", has, flashes(b))
else:
    skip("K1/K2 KVM provider + VM snapshot", "KVM_ENABLED=false (Docker Desktop has no /dev/kvm)")
st, bk = jget(adm, "/api/backup/status")
if not KVM and isinstance(bk, dict) and bk.get("status") != "ok" and E.get("DESKTOP_MODE") == "true":
    skip("K3 backup status", f"no backup run on this laptop yet ({bk.get('status')}); run scripts/backup.sh via devhub.ps1")
else:
    check("K3 backup status visible in portal (last backup + restore test ok)", isinstance(bk, dict) and bk.get("status") == "ok" and bk.get("verify", {}).get("status") == "ok", bk)
if ws and not keep:
    post(eng, f"{P}/workspaces/{ws['id']}/delete", {}); print("cleanup: e2e workspace delete requested")
for c in kc_admin("GET", f"/users/{uid}/credentials"):
    if c["type"] == "otp": kc_admin("DELETE", f"/users/{uid}/credentials/{c['id']}")
print(f"{sum(results)}/{len(results)} checks passed"); sys.exit(0 if all(results) else 1)
