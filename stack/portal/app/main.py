"""CSIR Software Development Hub - portal (FastAPI).
Sits in front of Coder (orchestrator) and adds: SSO/local login, six-role RBAC, workspace catalog and provisioning with
quotas + network profiles, VS Code / noVNC / brokered-RDP launch, secrets injection, projects, idle reclaim, FinOps
estimates, audit log, health and an admin console (users, workspaces, images, quotas)."""
import csv, io, json, logging, re, threading, time
from sqlite3 import IntegrityError
from typing import Annotated
from uuid import uuid4
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, HTMLResponse, PlainTextResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
import httpx
from . import db, auth, rbac, coder, quotas, reclaim, audit, guac, secrets_store
from .config import (POLICY, ROLE_IDS, HEALTH_TARGETS, CODER_PUBLIC_URL, GRAFANA_PUBLIC_URL, KEYCLOAK_PUBLIC_URL,
                     GUAC_PUBLIC_URL, HOST_IP)
from .metrics import LOGINS, WS_ACTIONS, QUOTA_DENIALS, USERS, WORKSPACES, COST_RATE, ALLOC_CPU, ALLOC_MEM
from . import blueprints, engine, execution_records, provisioning
from .browser_security import PortalSessions, csrf_token, verify_csrf
from .metrics import RECONCILE_FAILURES, RECONCILE_LAST_SUCCESS, RECONCILE_OK
from .extensions import get as ext
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("portal")
BASE = Path(__file__).resolve().parent
app = FastAPI(title="CSIR Dev Hub Portal", version="0.1.0", dependencies=[Depends(verify_csrf)])
from .config import SESSION_KEY
app.add_middleware(PortalSessions, secret_key=SESSION_KEY,
                   session_cookie="devhub_session", max_age=8 * 3600, same_site="lax")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
tpl = Jinja2Templates(directory=BASE / "templates")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")

def ip(request): return request.headers.get("x-forwarded-for", request.client.host if request.client else "")
def fmt_ts(t): return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "-"
tpl.env.filters["ts"] = fmt_ts
tpl.env.globals.update(can=rbac.can, CODER=CODER_PUBLIC_URL, GRAFANA=GRAFANA_PUBLIC_URL, KEYCLOAK=KEYCLOAK_PUBLIC_URL,
                       GUAC=GUAC_PUBLIC_URL, ROLE_DESC=rbac.DESCRIPTIONS, ROLE_IDS=ROLE_IDS)

def render(request, name, user, **ctx):
    flash = request.session.pop("flash", None)
    return tpl.TemplateResponse(request, name, {"user": user, "flash": flash, "csrf_token": csrf_token(request), **ctx})

def flash(request, msg, kind="ok"): request.session["flash"] = {"msg": msg, "kind": kind}

class LoginRequired(Exception): pass
@app.exception_handler(LoginRequired)
def _login_required(request, exc): return RedirectResponse("/login", 303)

def need(request, perm=None):
    u = auth.current_user(request)
    if not u: raise LoginRequired()
    if perm and not rbac.can(u, perm): raise HTTPException(403, f"requires permission '{perm}'")
    return u

# Phase 2 routes (licences, image scans, billing, KVM) are registered BEFORE the generic /admin/images/{template}
# route below, otherwise "/admin/images/scan" would be captured as template="scan".
from . import phase2, sdlc, sdlc_gate, sdlc_identity, sdlc_store  # noqa: E402
phase2.register(app, need, render, flash, ip)
sdlc_identity.register(app, render, flash, ip)
sdlc.register(app, render, flash, ip)
app.mount("/api/sdlc/automation", sdlc_gate.app())
tpl.env.globals.update(sdlc_project_enabled=sdlc_store.project_enabled, sdlc_enabled=sdlc_store.enabled)

# ------------------------------------------------------------------ startup
@app.on_event("startup")
def startup():
    db.init(); ext("licence").init(); ext("scanner").init(); auth.bootstrap_admin()
    reclaim.start()
    threading.Thread(target=_metrics_loop, daemon=True).start()
    log.info("portal started; coder healthy=%s", coder.healthy())

def _metrics_loop():
    while True:
        try: refresh_metrics()
        except Exception: log.exception("Portal metrics refresh failed")
        time.sleep(60)

def reconcile():
    try:
        live = _reconcile()
    except Exception:
        RECONCILE_FAILURES.inc()
        RECONCILE_OK.set(0)
        raise
    RECONCILE_LAST_SUCCESS.set(time.time())
    RECONCILE_OK.set(1)
    return live


def _reconcile():
    """Keep the portal ledger in sync with Coder: import workspaces created outside the portal (CLI/Coder UI) using their
    build parameters, and mark ledger rows deleted when the workspace no longer exists in Coder."""
    led = {r["id"]: r for r in db.q("SELECT * FROM workspaces WHERE deleted_at IS NULL")}
    requests = provisioning.pending()
    observed_at = time.time()
    live = {w["id"]: w for w in coder.workspaces() if coder.ws_status(w) != "deleted"}
    provisioning.recover(requests, live.values(), observed_at, coder.build_params)
    with db.tx() as c:
        for wid in set(led) - set(live):
            c.execute("UPDATE workspaces SET deleted_at=? WHERE id=?", (time.time(), wid))
    for wid in set(live) - set(led):
        with db.tx() as c:
            # Repair rows that an older portal marked deleted before the Coder build finished.
            if c.execute("UPDATE workspaces SET deleted_at=NULL WHERE id=?", (wid,)).rowcount:
                continue
        w = live[wid]; bp = coder.build_params(w["latest_build"]["id"])
        with db.tx() as c:
            c.execute("INSERT OR IGNORE INTO workspaces(id,owner,name,template,project_id,profile,cpu,mem_gb,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (wid, w["owner_name"], w["name"], w["template_name"], None, bp.get("network_profile", "dev"),
                       int(bp.get("cpu", 0) or 0), int(bp.get("memory", 0) or 0), "import", time.time()))
    deleted = {r["id"] for r in db.q("SELECT id FROM workspaces WHERE deleted_at IS NOT NULL")}
    ext("licence").reconcile(live, deleted=deleted)
    return live

def refresh_metrics():
    reconcile()
    for src in ("local", "oidc"):
        USERS.labels(source=src).set(db.one("SELECT COUNT(*) n FROM users WHERE auth_source=?", (src,))["n"])
    WORKSPACES.clear(); cpu = mem = 0
    led = {r["id"]: r for r in db.q("SELECT * FROM workspaces WHERE deleted_at IS NULL")}
    for ws in coder.workspaces():
        st = coder.ws_status(ws); WORKSPACES.labels(status=st, template=ws["template_name"]).inc()
        if st == "running" and ws["id"] in led: cpu += led[ws["id"]]["cpu"] or 0; mem += led[ws["id"]]["mem_gb"] or 0
    ALLOC_CPU.set(cpu); ALLOC_MEM.set(mem); COST_RATE.set(quotas.estimate(cpu, mem)["hourly"])

# ------------------------------------------------------------------ auth
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", None, oidc=auth.oidc_enabled())

@app.post("/login")
def login_local(request: Request, username: str = Form(...), password: str = Form(...)):
    row = auth.local_login(username.strip(), password)
    if not row:
        LOGINS.labels(method="local", outcome="fail").inc(); audit.log(username, "login.local", outcome="denied", ip=ip(request))
        flash(request, "Invalid username or password", "err"); return RedirectResponse("/login", 303)
    roles = json.loads(row["roles"])
    _after_login(request, row["username"], row["email"], row["full_name"], roles, "local", password=password)
    LOGINS.labels(method="local", outcome="ok").inc()
    return RedirectResponse("/", 303)

@app.get("/auth/oidc/login")
def oidc_login(request: Request):
    if not auth.oidc_enabled(): raise HTTPException(404, "OIDC not configured")
    return RedirectResponse(auth.oidc_start(request), 302)

@app.get("/auth/oidc/callback")
def oidc_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error: flash(request, f"SSO error: {error}", "err"); return RedirectResponse("/login", 303)
    try:
        claims, id_token = auth.oidc_finish(request, code, state)
    except Exception as e:
        LOGINS.labels(method="oidc", outcome="fail").inc(); audit.log("-", "login.oidc", detail=str(e), outcome="error", ip=ip(request))
        flash(request, f"SSO failed: {e}", "err"); return RedirectResponse("/login", 303)
    username = claims.get("preferred_username"); roles = rbac.roles_from_groups(claims.get("groups", []) + claims.get("realm_roles", []))
    if not roles: roles = ["guest"]
    with db.tx() as c:
        c.execute("""INSERT INTO users(username,email,full_name,auth_source,roles,created_at) VALUES (?,?,?,?,?,?)
                     ON CONFLICT(username) DO UPDATE SET email=excluded.email, full_name=excluded.full_name, roles=excluded.roles""",
                  (username, claims.get("email"), claims.get("name"), "oidc", json.dumps(roles), time.time()))
    request.session["id_token"] = id_token
    _after_login(request, username, claims.get("email"), claims.get("name"), roles, "oidc")
    LOGINS.labels(method="oidc", outcome="ok").inc()
    return RedirectResponse("/", 303)

def _after_login(request, username, email, name, roles, source, password=None):
    request.session.pop("external_sdlc", None)
    request.session["user"] = username
    with db.tx() as c: c.execute("UPDATE users SET last_login=? WHERE username=?", (time.time(), username))
    detail = {"roles": roles, "source": source}
    try:  # mirror identity + role mapping into Coder (OSS has no OIDC role sync -> portal does it)
        cu = coder.ensure_user(username, email, name, "oidc" if source == "oidc" else "password", password)
        if source == "local" and password and username != "admin":
            coder.set_password(username, password)
        want = rbac.coder_roles_for(roles)
        have = sorted(r["name"] for r in cu.get("roles", []))
        if username != "admin" and want != have: coder.set_site_roles(username, want); detail["coder_roles"] = want
    except Exception as e:
        detail["coder_sync_error"] = str(e)[:200]
    audit.log(username, f"login.{source}", detail=detail, ip=ip(request))

@app.post("/logout")
def logout(request: Request):
    u = request.session.get("user"); idt = request.session.get("id_token")
    src = (db.one("SELECT auth_source FROM users WHERE username=?", (u,)) or {}).get("auth_source")
    request.session.clear(); audit.log(u, "logout", ip=ip(request))
    return RedirectResponse(auth.logout_url(idt, request) if src == "oidc" else "/login", 303)

# ------------------------------------------------------------------ helpers
def catalog_items(user, include_unpublished=False):
    out = []
    for r in db.q("SELECT * FROM catalog ORDER BY template"):
        d = json.loads(r["data"]); d["published"] = bool(r["published"])
        if d["published"] or include_unpublished or rbac.can(user, "catalog.manage"): out.append(d)
    return out

def allowed_profiles(user, item):
    return [p for p in item["profiles"] if set(user["roles"]) & set(POLICY["network_profiles"][p]["roles"]) or rbac.can(user, "*")]


def catalog_image_blockers(item):
    scanner = ext("scanner")
    image = item["image"]
    pinned = item.get("pinned_image_id")
    if pinned:
        if scanner.image_id(image) != pinned:
            return ["Local image is missing or does not match the template's pinned image ID"]
        approval = scanner.approval(image, pinned)
        if not approval or approval["decision"] != "approved":
            return ["The pinned image must have an active publisher approval"]
    return scanner.gate(image)


def enrich(ws, ledger):
    l = ledger.get(ws["id"], {}); st = coder.ws_status(ws)
    item = next((json.loads(r["data"]) for r in db.q("SELECT data FROM catalog WHERE template=?", (ws["template_name"],))), {})
    ags = coder.agents(ws); apps = {a["slug"]: a for ag in ags for a in ag.get("apps", [])}
    return {"id": ws["id"], "name": ws["name"], "owner": ws["owner_name"], "template": ws["template_name"], "status": st,
            "transition": ws["latest_build"]["transition"], "agent": ags[0]["status"] if ags else "-",
            "cpu": l.get("cpu"), "mem_gb": l.get("mem_gb"), "profile": l.get("profile", "-"), "project_id": l.get("project_id"),
            "last_used": ws.get("last_used_at", "")[:16].replace("T", " "), "outdated": ws.get("outdated"),
            "vscode": coder.app_url(ws, "code-server") if "code-server" in apps else None,
            "novnc": coder.app_url(ws, "novnc") if "novnc" in apps else None,
            "rdp": "rdp" in item.get("access", []), "coder_url": f"{CODER_PUBLIC_URL}/@{ws['owner_name']}/{ws['name']}",
            "cost": quotas.estimate(l.get("cpu") or 0, l.get("mem_gb") or 0) if l else None}

def ledger_map(): return {r["id"]: r for r in db.q("SELECT * FROM workspaces")}

def user_projects(user):
    if rbac.can(user, "*"): return db.q("SELECT * FROM projects ORDER BY name")
    return db.q("""SELECT p.* FROM projects p LEFT JOIN project_members m ON m.project_id=p.id
                   WHERE p.owner=? OR m.username=? GROUP BY p.id ORDER BY p.name""", (user["username"], user["username"]))

# ------------------------------------------------------------------ dashboard / workspaces
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    u = need(request)
    led = ledger_map()
    try: mine = [enrich(w, led) for w in coder.workspaces(owner=u["username"])]; err = None
    except Exception as e: mine, err = [], str(e)
    q = quotas.effective_quota(u); us = quotas.usage(quotas.ledger(owner=u["username"]))
    return render(request, "home.html", u, workspaces=mine, quota=q, usage=us, error=err,
                  catalog=[dict(i, allowed=allowed_profiles(u, i)) for i in catalog_items(u)
                           if i["published"] or not i.get("pinned_image_id")],
                  projects=user_projects(u), rates=quotas.unit_rates())

@app.post("/workspaces")
def create_ws(request: Request, template: str = Form(...), name: str = Form(...), cpu: int = Form(2), memory: int = Form(2),
              profile: str = Form("dev"), project_id: str = Form(""), ubuntu_version: str = Form("24.04"), ide: str = Form("full"),
              blueprint_id: Annotated[int | None, Form()] = None):
    u = need(request, "workspace.own")
    name = name.strip().lower()
    def deny(reason, msg):
        QUOTA_DENIALS.labels(reason=reason).inc(); audit.log(u["username"], "workspace.create", f"{u['username']}/{name}", msg, ip(request), "denied")
        flash(request, msg, "err"); return RedirectResponse("/", 303)
    if not NAME_RE.match(name): return deny("name", "Name must be 3-32 chars: lowercase letters, digits, dashes")
    blueprint = None
    if blueprint_id is not None:
        blueprint = blueprints.get(blueprint_id)
        if not blueprint or blueprint["project_id"] not in {p["id"] for p in user_projects(u)}:
            raise HTTPException(404)
        spec = blueprint["spec"]
        template, profile = spec["template"], spec["profile"]
        cpu, memory = spec["cpu"], spec["memory"]
        project_id = str(blueprint["project_id"])
        ubuntu_version = spec["ubuntu_version"]
        ide = spec["ide"]
    item = next((i for i in catalog_items(u) if i["template"] == template), None)
    if not item or (item.get("pinned_image_id") and not item["published"]):
        return deny("catalog", f"Template '{template}' is not in the published catalog")
    if item.get("min_role") == "engineer" and set(u["roles"]) <= {"guest", "auditor"}: return deny("role", f"{item['display_name']} is not available to your role")
    if profile not in allowed_profiles(u, item): return deny("profile", f"Network profile '{profile}' not allowed for you on this template")
    if "guest" in u["roles"] and len(u["roles"]) == 1:
        if blueprint and (cpu > 1 or memory > 2):
            return deny("resources", "This blueprint exceeds the guest resource limit")
        cpu, memory = min(cpu, 1), min(memory, 2)
    if cpu < 1 or memory < 1: return deny("resources", "CPU and memory must be positive")
    try: project = db.one("SELECT * FROM projects WHERE id=?", (int(project_id),)) if project_id else None
    except ValueError: return deny("project", "Invalid project")
    if project_id and not project: return deny("project", "Project does not exist")
    if project and project["id"] not in [p["id"] for p in user_projects(u)]: return deny("project", "You are not a member of that project")
    from .extensions.scanner import policy as scan_policy
    if blueprint:
        if item.get("image") != spec["catalog_image"] or ext("scanner").image_id(spec["catalog_image"]) != spec["catalog_image_id"]:
            return deny("image", "The blueprint image changed or is unavailable. Ask the project owner for a new blueprint.")
    must_check_image = blueprint is not None or item.get("pinned_image_id") or scan_policy().get("enforce_on_create")
    gate = catalog_image_blockers(item) if item.get("image") and must_check_image else []
    if gate: return deny("image", "Image not approved: " + "; ".join(gate))
    owner = u["username"]
    request_id = uuid4().hex
    evidence = None
    if blueprint:
        evidence = {"schema_version": 1, "blueprint_id": blueprint["id"], "blueprint_name": blueprint["name"],
                    "request_id": request_id, "project_id": project["id"], "project": project["name"],
                    "workspace_name": name, "requested_by": owner, "requested_at": time.time(), "recipe": spec,
                    "cost_estimate": quotas.estimate(cpu, memory)}
    v = provisioning.reserve(u, name, template, profile, cpu, memory, project, request_id, evidence)
    if v: return deny("quota", "Quota exceeded: " + "; ".join(v))
    lic = ext("licence"); pool = lic.pool_for(profile, template)
    if pool:   # reserve a licence seat before provisioning; returned again if provisioning fails
        try: lic.checkout(pool, u, owner, workspace=f"{owner}/{name}", project=project, ip=ip(request), request_name=name, request_id=request_id)
        except Exception as e:
            provisioning.cancel(owner, name, request_id=request_id)
            return deny("licence", f"Licence: {e}")
    submitted = False
    try:
        coder.ensure_user(u["username"], u["email"], u["full_name"], "oidc" if u["auth_source"] == "oidc" else "password")
        if "rdp" in item.get("access", []): secrets_store.ensure_rdp_password(u["username"])
        definitions = coder.version_params(spec["template_version_id"]) if blueprint else coder.template_params(template)
        tparams = {p["name"] for p in definitions}
        defaults = spec["template_defaults"] if blueprint else item.get("params", {})
        params = {**defaults, "cpu": cpu, "memory": memory, "network_profile": profile, "ubuntu_version": ubuntu_version, "ide": ide}
        params = {k: v for k, v in params.items() if k in tparams}
        if blueprint and not {"devhub_request_id", "cpu", "memory", "network_profile"} <= tparams:
            raise ValueError("Blueprint template no longer supports the required provisioning parameters")
        if "devhub_request_id" in tparams:
            params["devhub_request_id"] = request_id
        if any(w["name"] == name for w in coder.workspaces(owner)):
            raise ValueError("A workspace with that name already exists")
        provisioning.submitted(owner, name, request_id)
        submitted = True
        version = {"template_version_id": spec["template_version_id"]} if blueprint else {}
        ws = coder.create_workspace(u["username"], template, name, params, ttl_ms=8 * 3600 * 1000, **version)
    except Exception as e:
        rejected = isinstance(e, coder.CoderError) and (
            e.status is None or (400 <= e.status < 500 and e.status != 408)
        )
        if not submitted or rejected:
            provisioning.cancel(owner, name, request_id=request_id)
        audit.log(u["username"], "workspace.create", f"{u['username']}/{name}", str(e)[:300], ip(request), "error")
        flash(request, f"Provisioning failed: {e}", "err"); return RedirectResponse("/", 303)
    provisioning.complete(ws, request_id=request_id)
    WS_ACTIONS.labels(action="create", template=template).inc()
    audit.log(u["username"], "workspace.create", f"{u['username']}/{name}", {"template": template, "cpu": cpu, "mem_gb": memory, "profile": profile,
              "project": project["name"] if project else None}, ip(request))
    flash(request, f"Workspace '{name}' is being provisioned ({item['display_name']}, {cpu} vCPU / {memory} GB, profile {profile}).")
    return RedirectResponse("/", 303)

def _ws_authorised(u, ws, manage=True):
    if ws["owner_name"] == u["username"]: return True
    return rbac.can(u, "workspace.manage_all" if manage else "workspace.view_all")

@app.post("/workspaces/{ws_id}/{action}")
def ws_action(request: Request, ws_id: str, action: str):
    u = need(request)
    if action not in ("start", "stop", "delete", "restart", "update"): raise HTTPException(400)
    ws = coder.workspace(ws_id)
    if not _ws_authorised(u, ws): raise HTTPException(403)
    lic = ext("licence"); led = db.one("SELECT * FROM workspaces WHERE id=?", (ws_id,)) or {}
    pool = lic.pool_for(led.get("profile"), ws["template_name"])
    if pool and action in ("start", "restart", "update") and not lic.seat_for_ws(ws_id):   # seat was released on stop
        owner = db.one("SELECT * FROM users WHERE username=?", (ws["owner_name"],)); owner = dict(owner, roles=json.loads(owner["roles"])) if owner else None
        proj = db.one("SELECT * FROM projects WHERE id=?", (led.get("project_id"),)) if led.get("project_id") else None
        try: lic.checkout(pool, owner, u["username"], workspace_id=ws_id, workspace=f"{ws['owner_name']}/{ws['name']}", project=proj, ip=ip(request))
        except Exception as e:
            flash(request, f"{action} denied - licence: {e}", "err"); return RedirectResponse(request.headers.get("referer", "/"), 303)
    try:
        if action == "restart": coder.build(ws_id, "stop"); time.sleep(2); coder.build(ws_id, "start")
        elif action == "update":
            t = coder.template_by_name(ws["template_name"])
            coder.req("POST", f"/api/v2/workspaces/{ws_id}/builds", json={"transition": "start", "template_version_id": t["active_version_id"]})
        else: coder.build(ws_id, action)
        WS_ACTIONS.labels(action=action, template=ws["template_name"]).inc()
        audit.log(u["username"], f"workspace.{action}", f"{ws['owner_name']}/{ws['name']}", ip=ip(request))
        flash(request, f"{action.capitalize()} requested for {ws['owner_name']}/{ws['name']}")
    except Exception as e:
        audit.log(u["username"], f"workspace.{action}", f"{ws['owner_name']}/{ws['name']}", str(e)[:300], ip(request), "error")
        flash(request, f"{action} failed: {e}", "err")
    return RedirectResponse(request.headers.get("referer", "/"), 303)

@app.get("/workspaces/{ws_id}/rdp")
def ws_rdp(request: Request, ws_id: str):
    """Brokered RDP: attach guacd to the workspace's private network, mint a 5-minute Guacamole token, redirect."""
    u = need(request)
    ws = coder.workspace(ws_id)
    if not _ws_authorised(u, ws): raise HTTPException(403)
    if coder.ws_status(ws) != "running": flash(request, "Start the workspace first", "err"); return RedirectResponse("/", 303)
    slug = f"{ws['owner_name']}-{ws['name']}".lower()
    meta = {m["key"]: m["value"] for r in ws.get("latest_build", {}).get("resources", []) for m in (r.get("metadata") or [])}
    pw = meta.get("rdp_password") or secrets_store.get(ws["owner_name"], "rdp_password")   # VM templates: per-workspace credential
    if not pw: flash(request, "No RDP credential for this workspace owner", "err"); return RedirectResponse("/", 303)
    item = next((json.loads(r["data"]) for r in db.q("SELECT data FROM catalog WHERE template=?", (ws["template_name"],))), {})
    try:
        if item.get("kind") == "vm":   # libvirt VM: DNAT on the libvirt host, reachable only from guacd
            host, port = ext("kvm").rdp_forward(f"csir-{slug}")
            url = guac.rdp_link(u["username"], ws, host, pw, None, port=port, rdp_user=item.get("rdp_user", "coder"),
                                security=item.get("rdp_security", "any"))
        else:
            url = guac.rdp_link(u["username"], ws, f"coder-{slug}", pw, f"csir-ws-{slug}")
    except Exception as e:
        flash(request, f"RDP broker error: {e}", "err"); return RedirectResponse("/", 303)
    audit.log(u["username"], "workspace.rdp", f"{ws['owner_name']}/{ws['name']}", "guacamole session token issued (5 min)", ip(request))
    return RedirectResponse(url, 302)

# ------------------------------------------------------------------ secrets
@app.get("/secrets", response_class=HTMLResponse)
def secrets_page(request: Request):
    u = need(request, "workspace.own")
    return render(request, "secrets.html", u, items=secrets_store.names(u["username"]))

@app.post("/secrets")
def secrets_put(request: Request, name: str = Form(...), value: str = Form(...)):
    u = need(request, "workspace.own")
    try:
        secrets_store.put(u["username"], name.strip(), value)
        audit.log(u["username"], "secret.put", name.strip(), ip=ip(request)); flash(request, f"Secret '{name}' saved and injected at /run/csir-secrets/{name}")
    except Exception as e: flash(request, f"Could not save secret: {e}", "err")
    return RedirectResponse("/secrets", 303)

@app.post("/secrets/delete")
def secrets_del(request: Request, name: str = Form(...)):
    u = need(request, "workspace.own")
    secrets_store.delete(u["username"], name); audit.log(u["username"], "secret.delete", name, ip=ip(request))
    flash(request, f"Secret '{name}' removed"); return RedirectResponse("/secrets", 303)

# ------------------------------------------------------------------ projects
@app.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request):
    u = need(request)
    projs = user_projects(u) if not rbac.can(u, "projects.view_workspaces") or not rbac.can(u, "audit.view") else db.q("SELECT * FROM projects ORDER BY name")
    for p in projs:
        p["members"] = [m["username"] for m in db.q("SELECT username FROM project_members WHERE project_id=?", (p["id"],))]
        p["usage"] = quotas.usage(quotas.ledger(project_id=p["id"]))
        p["cost"] = quotas.estimate(p["usage"]["cpu"], p["usage"]["mem_gb"])
        p["can_manage"] = rbac.can(u, "*") or (rbac.can(u, "projects.manage") and p["owner"] == u["username"])
    return render(request, "projects.html", u, projects=projs, users=[r["username"] for r in db.q("SELECT username FROM users ORDER BY username")])


def _blueprint_project(user, project_id):
    if any(p["id"] == project_id for p in user_projects(user)) or (
        rbac.can(user, "projects.view_workspaces") and rbac.can(user, "audit.view")
    ):
        project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
        if project:
            return project
    raise HTTPException(404)


@app.get("/projects/{pid}/blueprints", response_class=HTMLResponse)
def project_blueprints(request: Request, pid: int, blueprint: int | None = None):
    user = need(request)
    project = _blueprint_project(user, pid)
    recipes = blueprints.for_project(pid)
    if blueprint is not None:
        recipes = [recipe for recipe in recipes if recipe["id"] == blueprint]
        if not recipes:
            raise HTTPException(404)
    for recipe in recipes:
        recipe["records"] = blueprints.records(recipe["id"])
        recipe["comparison"] = execution_records.comparison(recipe["records"])
    can_manage = rbac.can(user, "*") or (rbac.can(user, "projects.manage") and project["owner"] == user["username"])
    options = [dict(item, allowed=allowed_profiles(user, item)) for item in catalog_items(user)
               if item.get("kind") == "container" and item.get("image")
               and (item["published"] or not item.get("pinned_image_id"))]
    may_request = rbac.can(user, "workspace.own") and any(p["id"] == pid for p in user_projects(user))
    may_review = rbac.can(user, "*")
    requests = db.q("""SELECT * FROM project_requests WHERE project_id=? AND (? OR requested_by=?)
                      ORDER BY requested_at DESC""", (pid, may_review, user["username"]))
    return render(request, "blueprints.html", user, project=project, recipes=recipes, selected=blueprint,
                  can_manage=can_manage, options=options, can_launch=may_request, may_review=may_review,
                  project_requests=requests)


@app.post("/projects/{pid}/blueprints")
def save_blueprint(request: Request, pid: int, name: str = Form(...), repository: str = Form(...),
                   revision: str = Form(...), template: str = Form(...), profile: str = Form(...),
                   cpu: int = Form(...), memory: int = Form(...)):
    user = need(request, "projects.manage")
    project = _blueprint_project(user, pid)
    if not (rbac.can(user, "*") or project["owner"] == user["username"]):
        raise HTTPException(403)
    try:
        repository, revision = blueprints.parse_source(repository, revision)
        item = next((i for i in catalog_items(user) if i["template"] == template), None)
        if (not item or item.get("kind") != "container" or not item.get("image")
                or (item.get("pinned_image_id") and not item["published"]) or profile not in allowed_profiles(user, item)):
            raise ValueError("Choose an eligible container template and network profile")
        if cpu < 1 or memory < 1:
            raise ValueError("CPU and memory must be positive")
        active = coder.template_by_name(template)
        if not active or not active.get("active_version_id"):
            raise ValueError("The Coder template has no active version")
        params = {p["name"] for p in coder.version_params(active["active_version_id"])}
        if not {"devhub_request_id", "cpu", "memory", "network_profile"} <= params:
            raise ValueError("Republish the current Dev Hub template before creating blueprints")
        image = item["image"]
        image_id = ext("scanner").image_id(image)
        gate = catalog_image_blockers(item)
        if not image_id or gate:
            raise ValueError("The catalog image is unavailable or blocked by image policy")
        spec = {"repository": repository, "revision": revision, "template": template, "profile": profile,
                "cpu": cpu, "memory": memory, "ubuntu_version": item.get("params", {}).get("ubuntu_version", "24.04"),
                "ide": "full", "template_version_id": active["active_version_id"], "catalog_image": image,
                "catalog_image_id": image_id, "template_defaults": item.get("params", {})}
        blueprint_id = blueprints.add(pid, name, spec, user["username"])
    except (IntegrityError, ValueError, coder.CoderError, httpx.RequestError) as error:
        flash(request, "A blueprint with that name already exists" if isinstance(error, IntegrityError) else str(error), "err")
        return RedirectResponse(f"/projects/{pid}/blueprints", 303)
    audit.log(user["username"], "project.blueprint.create", str(blueprint_id), {"project_id": pid}, ip(request))
    flash(request, "Blueprint saved")
    return RedirectResponse(f"/projects/{pid}/blueprints?blueprint={blueprint_id}", 303)


@app.get("/api/projects/{pid}/evidence/{workspace_id}")
def project_evidence(request: Request, pid: int, workspace_id: str):
    user = need(request)
    _blueprint_project(user, pid)
    row = db.one("""SELECT e.record FROM provisioning_evidence e
                    JOIN project_blueprints b ON b.id=e.blueprint_id
                    WHERE b.project_id=? AND e.workspace_id=?""", (pid, workspace_id))
    if not row:
        raise HTTPException(404)
    return JSONResponse({**json.loads(row["record"]), "workspace_id": workspace_id}, headers={"Cache-Control": "no-store"})


@app.post("/projects/{pid}/workspaces/{workspace_id}/execution-record")
def upload_execution_record(request: Request, pid: int, workspace_id: str, receipt: UploadFile = File(...)):
    user = need(request)
    _blueprint_project(user, pid)
    workspace = db.one(
        """SELECT b.id AS blueprint_id, e.record, w.owner FROM provisioning_evidence e
           JOIN project_blueprints b ON b.id=e.blueprint_id JOIN workspaces w ON w.id=e.workspace_id
           WHERE b.project_id=? AND e.workspace_id=?""", (pid, workspace_id),
    )
    if not workspace:
        raise HTTPException(404)
    if workspace["owner"] != user["username"]:
        raise HTTPException(403)
    target = f"/projects/{pid}/blueprints?blueprint={workspace['blueprint_id']}#provisioning-records"
    expected_commit = json.loads(workspace["record"])["recipe"]["revision"]
    try:
        record = execution_records.parse(receipt.file.read(execution_records.MAX_BYTES + 1), expected_commit)
    except ValueError as error:
        flash(request, str(error), "err")
        return RedirectResponse(target, 303)
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
    with db.tx(immediate=True) as conn:
        existing = db.one("SELECT record FROM workspace_execution_records WHERE workspace_id=?", (workspace_id,))
        if existing:
            if existing["record"] != canonical:
                flash(request, "This workspace already has an execution record. Launch a new workspace for a different run.", "err")
            else:
                flash(request, "This execution record is already attached")
            return RedirectResponse(target, 303)
        conn.execute(
            """INSERT INTO workspace_execution_records(workspace_id,record,submitted_by,submitted_at)
               VALUES (?,?,?,?)""", (workspace_id, canonical, user["username"], time.time()),
        )
    audit.log(user["username"], "project.execution.attach", workspace_id, {"project_id": pid}, ip(request))
    flash(request, "Execution record attached")
    return RedirectResponse(target, 303)


@app.get("/api/projects/{pid}/evidence/{workspace_id}/execution")
def project_execution_record(request: Request, pid: int, workspace_id: str):
    _blueprint_project(need(request), pid)
    row = db.one(
        """SELECT x.record FROM workspace_execution_records x
           JOIN provisioning_evidence e ON e.workspace_id=x.workspace_id
           JOIN project_blueprints b ON b.id=e.blueprint_id
           WHERE b.project_id=? AND x.workspace_id=?""", (pid, workspace_id),
    )
    if not row:
        raise HTTPException(404)
    return JSONResponse(json.loads(row["record"]), headers={"Cache-Control": "no-store",
                        "Content-Disposition": 'attachment; filename="execution-record.json"'})


@app.post("/projects/{pid}/requests")
def submit_project_request(request: Request, pid: int, kind: str = Form(...),
                           subject: str = Form(...), detail: str = Form(...)):
    user = need(request, "workspace.own")
    if not any(p["id"] == pid for p in user_projects(user)):
        raise HTTPException(404)
    subject, detail = subject.strip(), detail.strip()
    if kind not in {"dataset", "capacity", "support"} or not 1 <= len(subject) <= 160 or not 1 <= len(detail) <= 2000:
        raise HTTPException(422, "Choose a request type and supply a subject and details")
    with db.tx() as conn:
        request_id = conn.execute(
            """INSERT INTO project_requests(project_id,kind,subject,detail,requested_by,requested_at)
               VALUES (?,?,?,?,?,?)""", (pid, kind, subject, detail, user["username"], time.time()),
        ).lastrowid
    audit.log(user["username"], "project.request.submit", str(request_id), {"project_id": pid, "kind": kind}, ip(request))
    flash(request, "Request submitted for operator review")
    return RedirectResponse(f"/projects/{pid}/blueprints#project-requests", 303)


@app.post("/projects/{pid}/requests/{rid}/review")
def review_project_request(request: Request, pid: int, rid: int, decision: str = Form(...), note: str = Form(...)):
    user = need(request, "*")
    note = note.strip()
    if decision not in {"reviewed", "declined"} or not 1 <= len(note) <= 2000:
        raise HTTPException(422, "Choose a review outcome and enter a note")
    with db.tx() as conn:
        result = conn.execute(
            """UPDATE project_requests SET status=?, reviewed_by=?, reviewed_at=?, review_note=?
               WHERE id=? AND project_id=? AND status='submitted'""",
            (decision, user["username"], time.time(), note, rid, pid),
        )
        if not result.rowcount:
            raise HTTPException(409, "This request is unavailable or has already been reviewed")
    audit.log(user["username"], "project.request." + decision, str(rid), {"project_id": pid}, ip(request))
    flash(request, "Review recorded")
    return RedirectResponse(f"/projects/{pid}/blueprints#project-requests", 303)

@app.post("/projects")
def projects_create(request: Request, name: str = Form(...), cost_centre: str = Form(""), max_workspaces: int = Form(10),
                    max_cpu: int = Form(20), max_mem_gb: int = Form(40)):
    u = need(request, "projects.manage")
    try:
        with db.tx() as c:
            c.execute("INSERT INTO projects(name,owner,cost_centre,max_workspaces,max_cpu,max_mem_gb,created_at) VALUES (?,?,?,?,?,?,?)",
                      (name.strip(), u["username"], cost_centre, max_workspaces, max_cpu, max_mem_gb, time.time()))
        audit.log(u["username"], "project.create", name, {"quota": [max_workspaces, max_cpu, max_mem_gb]}, ip(request))
        flash(request, f"Project '{name}' created")
    except Exception as e: flash(request, f"Could not create project: {e}", "err")
    return RedirectResponse("/projects", 303)

@app.post("/projects/{pid}/members")
def projects_member(request: Request, pid: int, username: str = Form(...), op: str = Form("add")):
    u = need(request, "projects.manage")
    p = db.one("SELECT * FROM projects WHERE id=?", (pid,))
    if not p or not (rbac.can(u, "*") or p["owner"] == u["username"]): raise HTTPException(403)
    with db.tx() as c:
        if op == "add": c.execute("INSERT OR IGNORE INTO project_members VALUES (?,?)", (pid, username))
        elif op == "remove":
            c.execute("DELETE FROM project_members WHERE project_id=? AND username=?", (pid, username))
            c.execute("DELETE FROM sdlc_roles WHERE project_id=? AND username=?", (pid, username))
        else:
            raise HTTPException(422, "Choose add or remove")
    audit.log(u["username"], f"project.member.{op}", f"{p['name']}:{username}", ip=ip(request))
    return RedirectResponse("/projects", 303)

# ------------------------------------------------------------------ admin console
@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    u = need(request)
    if not any(rbac.can(u, p) for p in ("users.view", "catalog.manage", "audit.view", "health.view")): raise HTTPException(403)
    return RedirectResponse("/admin/workspaces" if rbac.can(u, "workspace.view_all") else "/admin/images", 303)

@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request):
    u = need(request, "users.view")
    portal = {r["username"]: dict(r, roles=json.loads(r["roles"])) for r in db.q("SELECT * FROM users")}
    try: cusers = coder.users()
    except Exception: cusers = []
    rows = []
    for cu in cusers:
        p = portal.pop(cu["username"], None)
        rows.append({"username": cu["username"], "email": cu["email"], "coder_status": cu["status"], "login_type": cu["login_type"],
                     "coder_roles": [r["name"] for r in cu["roles"]], "portal": p, "quota": quotas.effective_quota(p) if p else None,
                     "usage": quotas.usage(quotas.ledger(owner=cu["username"]))})
    for p in portal.values():
        rows.append({"username": p["username"], "email": p["email"], "coder_status": "-", "login_type": "-", "coder_roles": [],
                     "portal": p, "quota": quotas.effective_quota(p), "usage": quotas.usage(quotas.ledger(owner=p["username"]))})
    return render(request, "admin_users.html", u, rows=sorted(rows, key=lambda r: r["username"]))

@app.post("/admin/users")
def admin_user_create(request: Request, username: str = Form(...), email: str = Form(""), full_name: str = Form(""),
                      password: str = Form(...), roles: list[str] = Form(["engineer"])):
    u = need(request, "users.manage")
    username = username.strip().lower()
    if not NAME_RE.match(username): flash(request, "Invalid username", "err"); return RedirectResponse("/admin/users", 303)
    roles = [r for r in roles if r in ROLE_IDS] or ["engineer"]
    try:
        with db.tx() as c:
            c.execute("INSERT INTO users(username,email,full_name,auth_source,password_hash,roles,created_at) VALUES (?,?,?,?,?,?,?)",
                      (username, email or f"{username}@csir.local", full_name or username, "local", auth.hash_pw(password), json.dumps(roles), time.time()))
        cu = coder.ensure_user(username, email or f"{username}@csir.local", full_name or username, "password", password)
        want = rbac.coder_roles_for(roles)
        if want: coder.set_site_roles(username, want)
        audit.log(u["username"], "user.create", username, {"roles": roles, "source": "local"}, ip(request))
        flash(request, f"Local user '{username}' created with roles {', '.join(roles)} (Coder account linked)")
    except Exception as e: flash(request, f"Could not create user: {e}", "err")
    return RedirectResponse("/admin/users", 303)

@app.post("/admin/users/{username}/roles")
def admin_user_roles(request: Request, username: str, roles: list[str] = Form([])):
    u = need(request, "users.manage")
    row = db.one("SELECT * FROM users WHERE username=?", (username,))
    if not row: raise HTTPException(404)
    roles = [r for r in roles if r in ROLE_IDS]
    with db.tx() as c: c.execute("UPDATE users SET roles=? WHERE username=?", (json.dumps(roles), username))
    try: coder.set_site_roles(username, rbac.coder_roles_for(roles))
    except Exception: pass
    note = " (OIDC users: roles are re-read from LDAP groups at next login)" if row["auth_source"] == "oidc" else ""
    audit.log(u["username"], "user.roles", username, {"roles": roles}, ip(request)); flash(request, f"Roles for {username}: {', '.join(roles) or 'none'}{note}")
    return RedirectResponse("/admin/users", 303)

@app.post("/admin/users/{username}/status")
def admin_user_status(request: Request, username: str, disabled: int = Form(...)):
    u = need(request, "users.manage")
    with db.tx() as c: c.execute("UPDATE users SET disabled=? WHERE username=?", (disabled, username))
    try: coder.suspend(username, bool(disabled))
    except Exception: pass
    audit.log(u["username"], "user.disable" if disabled else "user.enable", username, ip=ip(request))
    return RedirectResponse("/admin/users", 303)

@app.get("/admin/workspaces", response_class=HTMLResponse)
def admin_workspaces(request: Request):
    u = need(request, "workspace.view_all")
    led = ledger_map()
    try: rows = [enrich(w, led) for w in coder.workspaces()]; err = None
    except Exception as e: rows, err = [], str(e)
    projects = {p["id"]: p["name"] for p in db.q("SELECT id,name FROM projects")}
    return render(request, "admin_workspaces.html", u, rows=rows, error=err, projects=projects, cap=quotas.capacity())

@app.get("/admin/images", response_class=HTMLResponse)
def admin_images(request: Request):
    u = need(request)
    if not (rbac.can(u, "images.view") or rbac.can(u, "catalog.manage")): raise HTTPException(403)
    try: tmpls = {t["name"]: t for t in coder.templates()}
    except Exception: tmpls = {}
    images = []
    try:
        for im in engine.call("images"):
            tag = im["tags"][0] if im["tags"] else None
            images.append({**im, "scan": ext("scanner").scan(tag or im["full_id"]),
                           "approval": ext("scanner").approval(tag) if tag else None,
                           "gate": ext("scanner").gate(tag) if tag else []})
    except Exception as e: images.append({"tags": [f"docker API unavailable: {e}"], "id": "", "size_mb": 0, "created": "", "kind": "", "scan": ""})
    from .extensions.scanner import policy as scan_policy
    return render(request, "admin_images.html", u, catalog=catalog_items(u, True), templates=tmpls, images=images,
                  scans=ext("scanner").scans(30), scan_policy=scan_policy())

@app.post("/admin/images/{template}")
def admin_image_update(request: Request, template: str, published: int = Form(0), default_cpu: int = Form(2), default_mem: int = Form(2),
                       description: str = Form("")):
    u = need(request, "catalog.manage")
    r = db.one("SELECT * FROM catalog WHERE template=?", (template,))
    if not r: raise HTTPException(404)
    d = json.loads(r["data"]); d.update(default_cpu=default_cpu, default_mem=default_mem, description=description or d["description"])
    if published and not coder.template_by_name(template):
        flash(request, f"Cannot publish '{template}': no Coder template with that name has been pushed yet", "err"); return RedirectResponse("/admin/images", 303)
    if published and d.get("image") and (not r["published"] or d.get("pinned_image_id")):
        blockers = catalog_image_blockers(d)
        if blockers:
            audit.log(u["username"], "catalog.publish", template, {"blocked": blockers}, ip(request), "denied")
            flash(request, f"Cannot publish '{template}': " + "; ".join(blockers), "err"); return RedirectResponse("/admin/images", 303)
    with db.tx() as c: c.execute("UPDATE catalog SET data=?, published=?, updated_by=?, updated_at=? WHERE template=?",
                                 (json.dumps(d), published, u["username"], time.time(), template))
    audit.log(u["username"], "catalog.update", template, {"published": bool(published), "cpu": default_cpu, "mem": default_mem}, ip(request))
    flash(request, f"Catalog entry '{template}' updated"); return RedirectResponse("/admin/images", 303)

@app.get("/admin/quotas", response_class=HTMLResponse)
def admin_quotas(request: Request):
    u = need(request)
    if not (rbac.can(u, "quotas.view") or rbac.can(u, "quotas.manage")): raise HTTPException(403)
    return render(request, "admin_quotas.html", u, role_quotas=POLICY["role_quotas"], overrides=db.q("SELECT * FROM user_quotas ORDER BY username"),
                  profiles=POLICY["network_profiles"], pools=ext("licence").pools(), role_seats=POLICY.get("licensing", {}).get("role_max_seats", {}),
                  users=[r["username"] for r in db.q("SELECT username FROM users ORDER BY username")])

@app.post("/admin/quotas")
def admin_quota_set(request: Request, username: str = Form(...), max_workspaces: int = Form(...), max_cpu: int = Form(...),
                    max_mem_gb: int = Form(...), note: str = Form(""), remove: int = Form(0)):
    u = need(request, "quotas.manage")
    with db.tx() as c:
        if remove: c.execute("DELETE FROM user_quotas WHERE username=?", (username,))
        else: c.execute("INSERT INTO user_quotas VALUES (?,?,?,?,?) ON CONFLICT(username) DO UPDATE SET max_workspaces=excluded.max_workspaces, max_cpu=excluded.max_cpu, max_mem_gb=excluded.max_mem_gb, note=excluded.note",
                        (username, max_workspaces, max_cpu, max_mem_gb, note))
    audit.log(u["username"], "quota.remove" if remove else "quota.set", username, {"ws": max_workspaces, "cpu": max_cpu, "mem": max_mem_gb, "note": note}, ip(request))
    flash(request, f"Quota for {username} {'removed' if remove else 'saved'}"); return RedirectResponse("/admin/quotas", 303)

@app.get("/admin/reclaim", response_class=HTMLResponse)
def admin_reclaim(request: Request):
    u = need(request, "workspace.view_all")
    try: plan = reclaim.evaluate(); err = None
    except Exception as e: plan, err = [], str(e)
    return render(request, "admin_reclaim.html", u, plan=plan, policy=reclaim.policy(), error=err)

@app.post("/admin/reclaim")
def admin_reclaim_run(request: Request):
    u = need(request, "reclaim.run")
    done = reclaim.apply(reclaim.evaluate(), actor=u["username"])
    flash(request, f"Reclaim applied: {len(done)} action(s)"); return RedirectResponse("/admin/reclaim", 303)

@app.post("/admin/reclaim/policy")
def admin_reclaim_policy(request: Request, idle_stop_minutes: int = Form(...), stopped_delete_days: int = Form(...),
                         guest_max_age_days: int = Form(...), auto_apply: int = Form(0), delete_enabled: int = Form(0)):
    u = need(request, "reclaim.run")
    p = dict(idle_stop_minutes=idle_stop_minutes, stopped_delete_days=stopped_delete_days, guest_max_age_days=guest_max_age_days,
             auto_apply=bool(auto_apply), delete_enabled=bool(delete_enabled))
    db.set_setting("reclaim_policy", p); audit.log(u["username"], "reclaim.policy", "", p, ip(request))
    flash(request, "Reclaim policy saved"); return RedirectResponse("/admin/reclaim", 303)

@app.get("/admin/audit")
def admin_audit(request: Request, actor: str = "", action: str = "", format: str = ""):
    u = need(request, "audit.view")
    rows = audit.recent(1000 if format == "csv" else 300, actor or None, action or None)
    if format == "csv":
        buf = io.StringIO(); w = csv.writer(buf); w.writerow(["time", "actor", "action", "target", "outcome", "ip", "detail"])
        for r in rows: w.writerow([fmt_ts(r["ts"]), r["actor"], r["action"], r["target"], r["outcome"], r["ip"], r["detail"]])
        audit.log(u["username"], "audit.export", "", {"rows": len(rows)}, ip(request))
        return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=devhub-audit.csv"})
    return render(request, "admin_audit.html", u, rows=rows, actor=actor, action=action)

def health_checks():
    last = RECONCILE_LAST_SUCCESS._value.get()
    age = max(0, int(time.time() - last)) if last else None
    out = [{"name": "Workspace reconciliation", "ok": bool(RECONCILE_OK._value.get() and age is not None and age < 300),
            "detail": f"Last success {age}s ago" if age is not None else "No successful run yet", "ms": 0}]
    for name, url in HEALTH_TARGETS.items():
        t0 = time.time()
        try: r = httpx.get(url, timeout=4, follow_redirects=True); ok = r.status_code < 400; detail = f"HTTP {r.status_code}"
        except Exception as e: ok, detail = False, type(e).__name__
        out.append({"name": name, "ok": ok, "detail": detail, "ms": int((time.time() - t0) * 1000)})
    try:
        import socket; s = socket.create_connection(("openldap", 389), 3); s.close(); out.append({"name": "OpenLDAP", "ok": True, "detail": "tcp/389 open", "ms": 0})
    except Exception as e: out.append({"name": "OpenLDAP", "ok": False, "detail": type(e).__name__, "ms": 0})
    try:
        v = engine.call("version"); out.append({"name": "Docker engine", "ok": True, "detail": v, "ms": 0})
    except Exception as e: out.append({"name": "Docker engine", "ok": False, "detail": type(e).__name__, "ms": 0})
    return out

@app.get("/admin/health", response_class=HTMLResponse)
def admin_health(request: Request):
    u = need(request, "health.view")
    try:
        ws = coder.workspaces(); agents = [a for w in ws for a in coder.agents(w)]
        summary = {"workspaces": len(ws), "running": sum(coder.ws_status(w) == "running" for w in ws),
                   "agents_connected": sum(a["status"] == "connected" for a in agents), "templates": len(coder.templates())}
    except Exception: summary = {}
    return render(request, "admin_health.html", u, checks=health_checks(), summary=summary,
                  extensions={k: v.name for k, v in __import__("app.extensions.base", fromlist=["REGISTRY"]).REGISTRY.items()})

@app.get("/admin/finops", response_class=HTMLResponse)
def admin_finops(request: Request, tier: str = ""):
    u = need(request, "finops.view")
    tid = tier or db.get_setting("finops_tier", POLICY["finops"]["active_tier"])
    per_user = {}
    for r in quotas.ledger():
        e = quotas.estimate(r["cpu"] or 0, r["mem_gb"] or 0, tid); d = per_user.setdefault(r["owner"], {"ws": 0, "cpu": 0, "mem": 0, "monthly": 0.0})
        d["ws"] += 1; d["cpu"] += r["cpu"] or 0; d["mem"] += r["mem_gb"] or 0; d["monthly"] += e["monthly"]
    per_project = []
    for p in db.q("SELECT * FROM projects"):
        us = quotas.usage(quotas.ledger(project_id=p["id"])); per_project.append({**p, **us, **quotas.estimate(us["cpu"], us["mem_gb"], tid)})
    return render(request, "admin_finops.html", u, tiers=POLICY["finops"]["tiers"], tid=tid, cap=quotas.capacity(tid),
                  rates=quotas.unit_rates(tid), per_user=per_user, per_project=per_project, finops=POLICY["finops"])

@app.post("/admin/finops")
def admin_finops_set(request: Request, tier: str = Form(...)):
    u = need(request, "*")
    db.set_setting("finops_tier", tier); audit.log(u["username"], "finops.tier", tier, ip=ip(request))
    return RedirectResponse("/admin/finops", 303)

# ------------------------------------------------------------------ JSON API + metrics
@app.get("/api/health")
def api_health(): return {"portal": "ok", "coder": coder.healthy()}


@app.get("/api/csrf")
def api_csrf(request: Request):
    return JSONResponse({"token": csrf_token(request)}, headers={"Cache-Control": "no-store"})

@app.get("/api/me")
def api_me(request: Request):
    u = need(request); return {"username": u["username"], "roles": u["roles"], "permissions": sorted(rbac.perms(u["roles"])),
                               "quota": quotas.effective_quota(u), "usage": quotas.usage(quotas.ledger(owner=u["username"]))}

@app.get("/api/workspaces")
def api_workspaces(request: Request):
    u = need(request); led = ledger_map()
    ws = coder.workspaces() if rbac.can(u, "workspace.view_all") else coder.workspaces(owner=u["username"])
    return [enrich(w, led) for w in ws]


@app.get("/metrics")
def metrics(): return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
