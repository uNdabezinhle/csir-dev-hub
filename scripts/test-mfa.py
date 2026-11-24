#!/usr/bin/env python3
"""MFA end-to-end verification (Keycloak flow 'csir-browser-mfa' -> Coder OIDC).
For a demo LDAP user: (1) password-only login lands on forced TOTP enrolment, no Coder session;
(2) enrol TOTP from the page secret -> Coder session issued; (3) re-login with a WRONG code -> refused, no session;
(4) re-login with the correct code -> Coder session. Cleanup removes the test OTP credential (unless --keep).
Usage: scripts/test-mfa.py [ldap-uid] [--keep]"""
import sys, os, re, html, json, time, hmac, hashlib, struct, base64, urllib.request, urllib.parse, http.cookiejar
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
S = dict(l.strip().split("=", 1) for l in open(f"{ROOT}/secrets/devhub.env") if "=" in l and not l.startswith("#"))
E = dict(l.strip().split("=", 1) for l in open(f"{ROOT}/.env") if "=" in l and not l.startswith("#"))
KC = f"http://{E['HOST_IP']}:{E['KEYCLOAK_PORT']}"; CODER = f"http://{E['HOST_IP']}:{E['CODER_PORT']}"
args = [a for a in sys.argv[1:] if not a.startswith("--")]; user = args[0] if args else "engineer2"; keep = "--keep" in sys.argv
def totp(secret, offset=0):
    sec = secret.replace(" ", "").upper(); key = base64.b32decode(sec + "=" * (-len(sec) % 8))
    h = hmac.new(key, struct.pack(">Q", int(time.time()) // 30 + offset), hashlib.sha1).digest(); o = h[-1] & 15
    return "%06d" % ((struct.unpack(">I", h[o:o+4])[0] & 0x7fffffff) % 1000000)
def admin(method, path, body=None):
    t = json.load(urllib.request.urlopen(f"{KC}/realms/master/protocol/openid-connect/token", urllib.parse.urlencode(
        {"grant_type": "password", "client_id": "admin-cli", "username": "admin", "password": S["KEYCLOAK_ADMIN_PASSWORD"]}).encode()))["access_token"]
    r = urllib.request.urlopen(urllib.request.Request(KC + "/admin/realms/csir" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None, headers={"Authorization": "Bearer " + t, "Content-Type": "application/json"}))
    d = r.read(); return json.loads(d) if d else None
def form_action(page): return html.unescape(re.search(r'action="([^"]+)"', page).group(1))
def password_step():
    jar = http.cookiejar.CookieJar(); op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    page = op.open(CODER + "/api/v2/users/oidc/callback").read().decode()
    r = op.open(form_action(page), urllib.parse.urlencode({"username": user, "password": S["LDAP_DEMO_PASSWORD"]}).encode())
    return jar, op, r, r.read().decode()
has_session = lambda jar: any(c.name == "coder_session_token" for c in jar)
results = []
def check(name, ok): results.append(ok); print(("PASS " if ok else "FAIL ") + name)
uid = admin("GET", f"/users?username={user}&exact=true")[0]["id"]
for c in admin("GET", f"/users/{uid}/credentials"):
    if c["type"] == "otp": admin("DELETE", f"/users/{uid}/credentials/{c['id']}")
try:
    jar, op, r, page = password_step()
    check("1. password accepted (LDAP) but NO Coder session before MFA; enrolment forced", not has_session(jar) and "totp" in page.lower())
    if "kc-totp-secret-key" not in page:
        m = re.search(r'href="([^"]*mode=manual[^"]*)"', page)
        page = op.open(urllib.parse.urljoin(r.geturl(), html.unescape(m.group(1)))).read().decode()
    secret = re.sub(r"<[^>]+>", "", re.search(r'id="kc-totp-secret-key"[^>]*>(.*?)</', page, re.S).group(1)).strip()
    r = op.open(form_action(page), urllib.parse.urlencode({"totp": totp(secret), "totpSecret": html.unescape(re.search(r'name="totpSecret"[^>]*value="([^"]*)"', page).group(1)),
                "mode": "manual", "userLabel": "mfa-selftest"}).encode()); r.read()
    check("2. TOTP enrolled -> Coder session issued", has_session(jar))
    jar, op, r, page = password_step()
    check("3a. second login: OTP challenge shown, no session yet", "otp" in page.lower() and not has_session(jar))
    bad = "%06d" % ((int(totp(secret)) + 1) % 1000000)
    r = op.open(form_action(page), urllib.parse.urlencode({"otp": bad}).encode()); page2 = r.read().decode()
    check("3b. WRONG OTP rejected -> no Coder session", not has_session(jar) and "invalid" in page2.lower())
    time.sleep(31)  # next TOTP window (Keycloak rejects code reuse)
    r = op.open(form_action(page2), urllib.parse.urlencode({"otp": totp(secret)}).encode()); r.read()
    check("4. correct OTP -> Coder session issued", has_session(jar))
finally:
    if not keep:
        for c in admin("GET", f"/users/{uid}/credentials"):
            if c["type"] == "otp": admin("DELETE", f"/users/{uid}/credentials/{c['id']}")
        print("cleanup: test OTP credential removed")
sys.exit(0 if all(results) and len(results) == 5 else 1)
