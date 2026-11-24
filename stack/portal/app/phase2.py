"""Phase 2 portal routes: licence broker, image scanning/approval, billing export, KVM inventory, backup status."""
import json, time, datetime as dt
from fastapi import Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from . import db, rbac, coder, quotas, audit
from .config import POLICY
from .extensions import get as ext

def _day(s, default):
    try: return dt.datetime.strptime(s, "%Y-%m-%d").timestamp() if s else default
    except ValueError: raise HTTPException(400, "dates must be YYYY-MM-DD")

def register(app, need, render, flash, ip):
    def back(url): return RedirectResponse(url, 303)

    # ------------------------------------------------------------ licences
    @app.get("/admin/licences", response_class=HTMLResponse)
    def admin_licences(request: Request):
        u = need(request, "licences.view"); lic = ext("licence")
        return render(request, "admin_licences.html", u, pools=lic.pools(), seats=lic.open_seats(), history=lic.history(50),
                      projects=db.q("SELECT * FROM projects ORDER BY name"), users=[r["username"] for r in db.q("SELECT username FROM users ORDER BY username")],
                      role_seats=POLICY.get("licensing", {}).get("role_max_seats", {}), profile_pool=POLICY.get("licensing", {}).get("profile_pool", {}))

    @app.post("/admin/licences/{pool}/resize")
    def licences_resize(request: Request, pool: str, seats: int = Form(...)):
        u = need(request, "*")
        try: ext("licence").resize(pool, seats, u["username"], ip(request)); flash(request, f"Pool {pool} resized to {seats} seats")
        except Exception as e: flash(request, str(e), "err")
        return back("/admin/licences")

    @app.post("/admin/licences/checkout")
    def licences_manual(request: Request, pool: str = Form(...), username: str = Form(...), note: str = Form(...)):
        """Manual assignment (e.g. a physical lab PC or a VM outside Coder). Subject to the same pool/role limits."""
        u = need(request, "*")
        row = db.one("SELECT * FROM users WHERE username=?", (username,))
        if not row: flash(request, f"unknown user {username}", "err"); return back("/admin/licences")
        try:
            s = ext("licence").checkout(pool, dict(row, roles=json.loads(row["roles"])), u["username"], kind="manual", note=note.strip(), ip=ip(request))
            flash(request, f"Seat #{s['id']} from {pool} assigned to {username}")
        except Exception as e: flash(request, f"Licence: {e}", "err")
        return back("/admin/licences")

    @app.post("/admin/licences/return/{seat_id}")
    def licences_return(request: Request, seat_id: int, reason: str = Form("returned by admin")):
        u = need(request, "*")
        s = ext("licence").release(u["username"], reason, seat_id=seat_id, ip=ip(request))
        flash(request, f"Seat #{seat_id} returned" if s else f"Seat #{seat_id} not checked out", "ok" if s else "err")
        return back("/admin/licences")

    @app.post("/admin/licences/project/{pid}")
    def licences_project(request: Request, pid: int, max_licences: str = Form("")):
        u = need(request, "*"); v = int(max_licences) if max_licences.strip() else None
        with db.tx() as c: c.execute("UPDATE projects SET max_licences=? WHERE id=?", (v, pid))
        audit.log(u["username"], "licence.project.quota", str(pid), {"max_licences": v}, ip(request))
        flash(request, "Project licence quota saved"); return back("/admin/licences")

    @app.get("/api/licences")
    def api_licences(request: Request):
        u = need(request)
        lic = ext("licence")
        if rbac.can(u, "licences.view"): return {"pools": lic.pools(), "open_seats": lic.open_seats()}
        return {"pools": [{k: p[k] for k in ("id", "product", "seats", "used", "available")} for p in lic.pools()],
                "my_seats": [s for s in lic.open_seats() if s["username"] == u["username"]]}

    # ------------------------------------------------------------ image scanning
    @app.post("/admin/images/scan")
    def images_scan(request: Request, image: str = Form(...)):
        u = need(request, "images.scan")
        try: sid = ext("scanner").start_scan(image.strip(), u["username"], ip(request)); flash(request, f"Scan #{sid} of {image} started (Trivy)")
        except Exception as e: flash(request, f"Scan failed to start: {e}", "err")
        return back("/admin/images")

    @app.post("/admin/images/decide")
    def images_decide(request: Request, image: str = Form(...), decision: str = Form(...), note: str = Form(""), risk_accept: int = Form(0)):
        u = need(request, "images.approve")
        try: ext("scanner").decide(image, decision, u, note, bool(risk_accept), ip(request)); flash(request, f"{image}: {decision}")
        except Exception as e:
            audit.log(u["username"], f"image.{decision}", image, str(e)[:200], ip(request), "denied"); flash(request, f"{image}: {e}", "err")
        return back("/admin/images")

    @app.post("/admin/images/policy")
    def images_policy(request: Request, block_on_critical: int = Form(0), max_critical: int = Form(0), require_approval: int = Form(0),
                      enforce_on_create: int = Form(0)):
        u = need(request, "*")
        p = {"block_on_critical": bool(block_on_critical), "max_critical": max(0, max_critical), "require_approval": bool(require_approval),
             "enforce_on_create": bool(enforce_on_create)}
        db.set_setting("image_scanning", p); audit.log(u["username"], "image.policy", "", p, ip(request))
        flash(request, "Image scanning policy saved"); return back("/admin/images")

    @app.get("/admin/images/scans/{sid}", response_class=HTMLResponse)
    def images_scan_detail(request: Request, sid: int):
        u = need(request, "images.view"); s = ext("scanner").scan(sid=sid)
        if s["status"] == "not-scanned": raise HTTPException(404)
        return render(request, "admin_scan.html", u, s=s, vulns=ext("scanner").report(sid)[:2000])

    @app.get("/api/images/scans")
    def api_scans(request: Request):
        need(request, "images.view"); sc = ext("scanner")
        return {"policy": __import__("app.extensions.scanner", fromlist=["policy"]).policy(), "scans": sc.scans(100)}

    @app.get("/api/images/scans/{sid}")
    def api_scan(request: Request, sid: int):
        need(request, "images.view"); s = ext("scanner").scan(sid=sid)
        if s["status"] == "not-scanned": raise HTTPException(404)
        return {**s, "vulnerabilities": ext("scanner").report(sid)}

    @app.get("/api/images/gate")
    def api_gate(request: Request, image: str):
        need(request, "images.view"); sc = ext("scanner")
        return {"image": image, "publishable": not sc.gate(image), "blockers": sc.gate(image), "approval": sc.approval(image)}

    # ------------------------------------------------------------ billing / FinOps export
    def _billing(u, frm, to, group_by, tier):
        now = time.time(); start = _day(frm, now - 30 * 86400); end = _day(to, now) + (86400 if to else 0)
        if group_by not in ("workspace", "user", "project"): raise HTTPException(400, "group_by must be workspace|user|project")
        b = ext("billing"); rows, rate, prom_ok = b.usage(start, end, tier or None)
        if not (rbac.can(u, "*") or rbac.can(u, "audit.view")):   # project owners: only their projects
            mine = {p["name"] for p in db.q("SELECT name FROM projects WHERE owner=?", (u["username"],))}
            rows = [r for r in rows if r["project"] in mine]
        meta = {"from": dt.datetime.fromtimestamp(start).strftime("%Y-%m-%d"), "to": dt.datetime.fromtimestamp(end - 1).strftime("%Y-%m-%d"),
                "group_by": group_by, "rates": rate, "prometheus": prom_ok, "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
                "totals": {k: round(sum(r[k] or 0 for r in rows), 2) for k in ("running_hours", "vcpu_hours", "gb_hours", "cost_compute", "cost_storage", "cost_licence", "cost_total")}}
        return b.grouped(rows, group_by), meta

    @app.get("/api/billing/usage")
    def api_billing(request: Request, group_by: str = "workspace", format: str = "json", tier: str = ""):
        u = need(request, "finops.view"); qp = request.query_params
        rows, meta = _billing(u, qp.get("from", ""), qp.get("to", ""), group_by, tier)
        audit.log(u["username"], "billing.export", group_by, {"format": format, "from": meta["from"], "to": meta["to"], "rows": len(rows)}, ip(request))
        if format == "csv":
            return Response(ext("billing").export(rows, "csv", meta), media_type="text/csv",
                            headers={"Content-Disposition": f"attachment; filename=devhub-billing-{group_by}-{meta['from']}_{meta['to']}.csv"})
        return JSONResponse(ext("billing").export(rows, "json", meta))

    @app.get("/admin/billing", response_class=HTMLResponse)
    def admin_billing(request: Request, group_by: str = "user"):
        u = need(request, "finops.view"); qp = request.query_params
        rows, meta = _billing(u, qp.get("from", ""), qp.get("to", ""), group_by, "")
        return render(request, "admin_billing.html", u, rows=rows, meta=meta, group_by=group_by,
                      frm=qp.get("from", meta["from"]), to=qp.get("to", meta["to"]))

    @app.post("/admin/reconcile")
    def admin_reconcile(request: Request):
        """Sync the portal ledger + licence seats with Coder now (normally every 60 s)."""
        u = need(request, "*"); from .main import reconcile
        before = {s["id"] for s in ext("licence").open_seats()}
        live = reconcile(); after = {s["id"] for s in ext("licence").open_seats()}
        audit.log(u["username"], "reconcile", "", {"workspaces": len(live), "seats_returned": sorted(before - after)}, ip(request))
        return {"workspaces": len(live), "seats_returned": sorted(before - after)}

    # ------------------------------------------------------------ KVM
    @app.get("/admin/kvm", response_class=HTMLResponse)
    def admin_kvm(request: Request):
        u = need(request)
        if not (rbac.can(u, "kvm.view") or rbac.can(u, "health.view")): raise HTTPException(403)
        k = ext("kvm"); cap = k.capabilities()
        return render(request, "admin_kvm.html", u, cap=cap, domains=k.domains() if cap.get("status") == "ok" else [],
                      backup=ext("backup").status())

    @app.post("/admin/kvm/{domain}/{action}")
    def admin_kvm_action(request: Request, domain: str, action: str, snapshot: str = Form("")):
        u = need(request, "*"); k = ext("kvm")
        try:
            if action == "snapshot": k.snapshot(domain, snapshot or time.strftime("portal-%Y%m%d-%H%M%S"))
            elif action == "revert": k.revert(domain, snapshot)
            elif action == "delete-snapshot": k.delete_snapshot(domain, snapshot)
            else: k.power(domain, action)
            audit.log(u["username"], f"kvm.{action}", domain, {"snapshot": snapshot} if snapshot else None, ip(request))
            flash(request, f"{domain}: {action} ok")
        except Exception as e:
            audit.log(u["username"], f"kvm.{action}", domain, str(e)[:200], ip(request), "error"); flash(request, f"{domain}: {e}", "err")
        return back("/admin/kvm")

    @app.get("/api/kvm")
    def api_kvm(request: Request):
        u = need(request)
        if not (rbac.can(u, "kvm.view") or rbac.can(u, "health.view")): raise HTTPException(403)
        k = ext("kvm"); cap = k.capabilities(); return {"capabilities": cap, "domains": k.domains() if cap.get("status") == "ok" else []}

    @app.get("/api/backup/status")
    def api_backup(request: Request):
        need(request, "health.view"); return ext("backup").status()
