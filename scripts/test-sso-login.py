#!/usr/bin/env python3
"""End-to-end SSO check: Coder -> Keycloak (LDAP-federated user) -> Coder session.
Usage: scripts/test-sso-login.py <ldap-uid> [coder|portal]   (password read from secrets/devhub.env LDAP_DEMO_PASSWORD)"""
import sys, os, re, html, urllib.request, urllib.parse, http.cookiejar, json
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
S = dict(l.strip().split("=", 1) for l in open(f"{ROOT}/secrets/devhub.env") if "=" in l and not l.startswith("#"))
E = dict(l.strip().split("=", 1) for l in open(f"{ROOT}/.env") if "=" in l and not l.startswith("#"))
user = sys.argv[1] if len(sys.argv) > 1 else "engineer1"; target = sys.argv[2] if len(sys.argv) > 2 else "coder"
jar = http.cookiejar.CookieJar(); op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
start = (f"http://{E['HOST_IP']}:{E['CODER_PORT']}/api/v2/users/oidc/callback" if target == "coder"
         else f"http://{E['HOST_IP']}:{E['ADMIN_CONSOLE_PORT']}/auth/oidc/login")
r = op.open(start); page = r.read().decode()
m = re.search(r'action="([^"]+)"', page)
if not m: sys.exit("no Keycloak login form; landed on " + r.geturl())
r = op.open(html.unescape(m.group(1)), urllib.parse.urlencode({"username": user, "password": S["LDAP_DEMO_PASSWORD"], "credentialId": ""}).encode())
final = r.geturl(); body = r.read().decode(errors="ignore")
if "otp" in body.lower() and "kc-totp" in body.lower() or "CONFIGURE_TOTP" in final or "totp" in body.lower() and "login-actions" in final:
    print(f"{user}: password OK via LDAP; Keycloak now requires TOTP enrolment/verification (MFA) -> {final.split('?')[0]}"); sys.exit(0)
cookies = {c.name: c.value for c in jar}
if target == "coder":
    tok = cookies.get("coder_session_token")
    if not tok: sys.exit(f"FAILED: no coder session; final URL {final}")
    me = json.load(op.open(urllib.request.Request(f"http://{E['HOST_IP']}:{E['CODER_PORT']}/api/v2/users/me", headers={"Coder-Session-Token": tok})))
    print(f"{user}: SSO OK -> Coder user '{me['username']}' login_type={me['login_type']} roles={[x['name'] for x in me['roles']]}")
else:
    print(f"{user}: portal SSO final URL {final}; cookies={list(cookies)}")
