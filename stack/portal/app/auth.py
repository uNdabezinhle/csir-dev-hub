"""Authentication: local accounts (bcrypt) and OIDC (Keycloak; LDAP-federated users, TOTP enforced by the realm flow)."""
import time, secrets, bcrypt, httpx, jwt, json
from urllib.parse import urlencode, urlparse
from fastapi import Request
from . import db
from .config import OIDC_ISSUER, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, PORTAL_PUBLIC_URL, PORTAL_LOCAL_URL, BOOTSTRAP_ADMIN_PASSWORD

def hash_pw(pw): return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
def check_pw(pw, h): return bool(h) and bcrypt.checkpw(pw.encode(), h.encode())

def bootstrap_admin():
    """Local break-glass hub-admin 'admin' (same password as the Coder admin, from secrets)."""
    if BOOTSTRAP_ADMIN_PASSWORD and not db.one("SELECT 1 FROM users WHERE username='admin'"):
        with db.tx() as c:
            c.execute("INSERT INTO users(username,email,full_name,auth_source,password_hash,roles,created_at) VALUES (?,?,?,?,?,?,?)",
                      ("admin", "admin@csir.local", "CSIR Dev Hub Admin", "local", hash_pw(BOOTSTRAP_ADMIN_PASSWORD),
                       json.dumps(["hub-admin"]), time.time()))

def current_user(request: Request):
    u = request.session.get("user")
    if not u: return None
    row = db.one("SELECT * FROM users WHERE username=?", (u,))
    if not row or row["disabled"]: return None
    row["roles"] = json.loads(row["roles"] or "[]")
    return row

def local_login(username, password):
    row = db.one("SELECT * FROM users WHERE username=? AND auth_source='local'", (username,))
    if row and not row["disabled"] and check_pw(password, row["password_hash"]): return row
    return None

# ---------------------------------------------------------------- OIDC (authorization code flow, signature-verified id_token)
_disco = {}
def discovery():
    if not _disco and OIDC_ISSUER:
        _disco.update(httpx.get(f"{OIDC_ISSUER}/.well-known/openid-configuration", timeout=10).json())
    return _disco

def oidc_enabled(): return bool(OIDC_ISSUER and OIDC_CLIENT_SECRET)
def portal_base(request: Request = None):
    """Browser-facing base URL: the LAN URL if the request arrived on it, else the public URL."""
    if request is not None and PORTAL_LOCAL_URL and urlparse(PORTAL_LOCAL_URL).netloc == request.headers.get("host", ""):
        return PORTAL_LOCAL_URL
    return PORTAL_PUBLIC_URL

def redirect_uri(request: Request = None): return f"{portal_base(request)}/auth/oidc/callback"

def oidc_start(request: Request):
    state, nonce = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    request.session["oidc"] = {"state": state, "nonce": nonce, "redirect_uri": redirect_uri(request)}
    return discovery()["authorization_endpoint"] + "?" + urlencode({
        "client_id": OIDC_CLIENT_ID, "response_type": "code", "scope": "openid profile email",
        "redirect_uri": request.session["oidc"]["redirect_uri"], "state": state, "nonce": nonce})

def oidc_finish(request: Request, code, state):
    st = request.session.pop("oidc", None)
    if not st or st["state"] != state: raise ValueError("invalid OIDC state")
    d = discovery()
    tok = httpx.post(d["token_endpoint"], data={"grant_type": "authorization_code", "code": code, "redirect_uri": st.get("redirect_uri") or redirect_uri(),
                     "client_id": OIDC_CLIENT_ID, "client_secret": OIDC_CLIENT_SECRET}, timeout=15).json()
    if "id_token" not in tok: raise ValueError(f"token exchange failed: {tok.get('error_description', tok)}")
    key = jwt.PyJWKClient(d["jwks_uri"]).get_signing_key_from_jwt(tok["id_token"]).key
    claims = jwt.decode(tok["id_token"], key, algorithms=["RS256"], audience=OIDC_CLIENT_ID, issuer=d["issuer"])
    if claims.get("nonce") != st["nonce"]: raise ValueError("nonce mismatch")
    access = jwt.decode(tok["access_token"], options={"verify_signature": False})
    claims["realm_roles"] = access.get("realm_access", {}).get("roles", [])
    claims["acr"] = claims.get("acr")
    return claims, tok.get("id_token")

def logout_url(id_token=None, request: Request = None):
    if not oidc_enabled(): return "/login"
    p = {"post_logout_redirect_uri": f"{portal_base(request)}/login", "client_id": OIDC_CLIENT_ID}
    if id_token: p["id_token_hint"] = id_token
    return discovery()["end_session_endpoint"] + "?" + urlencode(p)
