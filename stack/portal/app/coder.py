"""Thin client for the Coder REST API (portal acts with an owner-scoped service token)."""
import httpx, secrets, string
from .config import CODER_URL, CODER_TOKEN, CODER_PUBLIC_URL

class CoderError(Exception): pass

def _c():
    return httpx.Client(base_url=CODER_URL, headers={"Coder-Session-Token": CODER_TOKEN}, timeout=30)

def req(method, path, **kw):
    with _c() as c:
        r = c.request(method, path, **kw)
    if r.status_code >= 400:
        try: msg = r.json().get("message", r.text) + " " + str(r.json().get("detail", ""))
        except Exception: msg = r.text
        raise CoderError(f"{method} {path} -> {r.status_code}: {msg.strip()[:400]}")
    return r.json() if r.content else None

def healthy():
    try:
        with httpx.Client(timeout=3) as c: return c.get(f"{CODER_URL}/healthz").text.strip() == "OK"
    except Exception: return False

def default_org():
    return req("GET", "/api/v2/organizations")[0]

def users():
    return req("GET", "/api/v2/users", params={"limit": 500}).get("users", [])

def get_user(username):
    try: return req("GET", f"/api/v2/users/{username}")
    except CoderError: return None

def ensure_user(username, email, full_name, login_type, password=None):
    u = get_user(username)
    if u: return u
    body = {"username": username, "email": email or f"{username}@csir.local", "name": full_name or username,
            "login_type": login_type, "organization_ids": [default_org()["id"]]}
    if login_type == "password":
        body["password"] = password or "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24)) + "aA1!"
    return req("POST", "/api/v2/users", json=body)

def set_password(username, password):
    req("PUT", f"/api/v2/users/{username}/password", json={"password": password, "old_password": ""})

def set_site_roles(username, roles):
    return req("PUT", f"/api/v2/users/{username}/roles", json={"roles": roles})

def suspend(username, suspended=True):
    return req("PUT", f"/api/v2/users/{username}/status/{'suspend' if suspended else 'activate'}")

def templates():
    org = default_org()["id"]
    return req("GET", f"/api/v2/organizations/{org}/templates")

def template_by_name(name):
    org = default_org()["id"]
    try: return req("GET", f"/api/v2/organizations/{org}/templates/{name}")
    except CoderError: return None

def template_params(template):
    t = template_by_name(template)
    if not t: return []
    return req("GET", f"/api/v2/templateversions/{t['active_version_id']}/rich-parameters")

def workspaces(owner=None):
    params = {"limit": 500}
    if owner: params["q"] = f"owner:{owner}"
    return req("GET", "/api/v2/workspaces", params=params).get("workspaces", [])

def workspace(ws_id):
    return req("GET", f"/api/v2/workspaces/{ws_id}")

def create_workspace(owner, template, name, params, ttl_ms=None):
    t = template_by_name(template)
    if not t: raise CoderError(f"template '{template}' is not published in Coder")
    body = {"template_id": t["id"], "name": name,
            "rich_parameter_values": [{"name": k, "value": str(v)} for k, v in params.items()]}
    if ttl_ms: body["ttl_ms"] = ttl_ms
    return req("POST", f"/api/v2/users/{owner}/workspaces", json=body)

def build(ws_id, transition):
    return req("POST", f"/api/v2/workspaces/{ws_id}/builds", json={"transition": transition})

def build_params(build_id):
    try: return {p["name"]: p["value"] for p in req("GET", f"/api/v2/workspacebuilds/{build_id}/parameters")}
    except CoderError: return {}

def app_url(ws, slug):
    return f"{CODER_PUBLIC_URL}/@{ws['owner_name']}/{ws['name']}.main/apps/{slug}/"

def ws_status(ws):
    return ws.get("latest_build", {}).get("status", "unknown")

def agents(ws):
    out = []
    for r in ws.get("latest_build", {}).get("resources", []):
        out += r.get("agents", []) or []
    return out
