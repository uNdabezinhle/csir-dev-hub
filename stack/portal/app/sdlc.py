"""Native project work, milestones, and human release review."""
import hashlib
import re
import json
import time
from datetime import date
from urllib.parse import urlsplit

from fastapi import Form, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import audit, db, sdlc_store as store

KINDS = ("requirement", "task", "bug", "test")
STATUSES = ("backlog", "in-progress", "review", "done")
PRIORITIES = ("low", "normal", "high")
REVIEW_ROLES = {"qa": "qa", "release": "release"}
APPROVAL_SECONDS = 72 * 3600


def text(value, limit, required=True):
    cleaned = value.strip()
    if len(cleaned) > limit or (required and not cleaned):
        raise HTTPException(422, f"Expected text of up to {limit} characters")
    return cleaned


def https_url(value, required=True):
    value = text(value, 2048, required)
    if not value:
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme == "https" and parts.hostname and parts.username is None
                and parts.password is None and not any(c.isspace() for c in value) and "\\" not in value):
            return value
    except ValueError:
        pass
    raise HTTPException(422, "Use an HTTPS link without embedded credentials")


def row(table, pid, identifier):
    # Table names are fixed by the callers below.
    value = db.one(f"SELECT * FROM {table} WHERE project_id=? AND id=?", (pid, identifier))
    if not value:
        raise HTTPException(404)
    return value


def milestone_tests(release):
    return db.q("""SELECT number,title,description,status,reference_url FROM sdlc_work
                   WHERE project_id=? AND milestone_id=? AND kind='test' ORDER BY number""",
                (release["project_id"], release["milestone_id"]))


def evidence_hash(tests):
    return hashlib.sha256(json.dumps(tests, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def release_blockers(release, now=None):
    now = time.time() if now is None else now
    project = db.one("SELECT * FROM projects WHERE id=?", (release["project_id"],))
    reviews = {review["kind"]: review for review in db.q(
        "SELECT * FROM sdlc_reviews WHERE release_id=?", (release["id"],)
    )}
    blockers = []
    tests = milestone_tests(release)
    expected = evidence_hash(tests)
    if not tests or any(test["status"] != "done" or not test["reference_url"] for test in tests):
        blockers.append("Complete all milestone test cases and link their evidence")
    actors = {release["created_by"]}
    for kind, role in REVIEW_ROLES.items():
        review = reviews.get(kind)
        if not review or not review["approved"]:
            blockers.append(f"{kind.upper()} approval is required")
            continue
        if review["reviewed_at"] <= now - APPROVAL_SECONDS:
            blockers.append(f"{kind.upper()} approval is older than 72 hours")
            continue
        if review["evidence_hash"] != expected:
            blockers.append(f"{kind.upper()} must review the changed milestone test evidence")
            continue
        actor = review["actor"]
        user = db.one("SELECT username,roles FROM users WHERE username=? AND disabled=0", (actor,))
        if user:
            user["roles"] = json.loads(user["roles"])
        if actor in actors or not project or store.internal_role(project, user) != role:
            blockers.append(f"{kind.upper()} approval is no longer eligible")
        actors.add(actor)
    return blockers


def register(app, render, flash, ip):
    def go(pid, tab):
        return RedirectResponse(f"/delivery/{pid}?tab={tab}", 303)

    def browser_error(request, message, code):
        response = render(request, "sdlc_error.html", None, message=message)
        response.status_code = code
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(StarletteHTTPException)
    async def delivery_http_error(request: Request, exc):
        if request.url.path.startswith("/delivery/") and "text/html" in request.headers.get("accept", ""):
            message = exc.detail if isinstance(exc.detail, str) else "The request could not be completed."
            return browser_error(request, message, exc.status_code)
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def delivery_form_error(request: Request, exc):
        if request.url.path.startswith("/delivery/") and "text/html" in request.headers.get("accept", ""):
            return browser_error(request, "Check that the required fields are complete and in the expected format.", 422)
        return await request_validation_exception_handler(request, exc)

    @app.get("/delivery/{pid}")
    def delivery(request: Request, pid: int, tab: str = "overview", view: str = "list",
                 status: str = "", kind: str = "", milestone: str = ""):
        project, user, role = store.project_access(request, pid)
        if tab not in {"overview", "work", "milestones", "releases", "people"}:
            raise HTTPException(404)
        if tab == "people" and role != "lead":
            raise HTTPException(403)
        work = db.q("SELECT * FROM sdlc_work WHERE project_id=? ORDER BY number DESC", (pid,))
        milestones = db.q("SELECT * FROM sdlc_milestones WHERE project_id=? ORDER BY due_on IS NULL,due_on,id", (pid,))
        releases = db.q("SELECT * FROM sdlc_releases WHERE project_id=? ORDER BY id DESC", (pid,))
        for release in releases:
            release["blockers"] = release_blockers(release)
            release["reviews"] = db.q("SELECT * FROM sdlc_reviews WHERE release_id=?", (release["id"],))
            release["deployments"] = db.q(
                "SELECT run_id,attempt,consumed_at,outcome,rollback FROM sdlc_permits WHERE release_id=? AND consumed_at IS NOT NULL ORDER BY consumed_at DESC",
                (release["id"],),
            )
        if milestone and (not milestone.isdecimal() or len(milestone) > 16):
            raise HTTPException(422, "Choose a valid milestone")
        comments = {}
        if tab == "work":
            for entry in db.q("SELECT * FROM sdlc_comments WHERE project_id=? ORDER BY id", (pid,)):
                comments.setdefault(entry["work_number"], []).append(entry)
        members = db.q(
            """SELECT m.username, coalesce(r.role,'reader') AS role FROM project_members m
               LEFT JOIN sdlc_roles r USING(project_id,username) WHERE m.project_id=? ORDER BY m.username""", (pid,)
        ) if role == "lead" or user["auth_source"] != "external" else []
        external = db.q(
            "SELECT * FROM sdlc_external WHERE project_id=? ORDER BY email", (pid,)
        ) if tab == "people" else []
        pending = db.q("""SELECT email,role,expires_at,token_hash FROM sdlc_invitations WHERE project_id=?
                          AND redeemed_at IS NULL AND revoked_at IS NULL AND expires_at>? ORDER BY expires_at""",
                       (pid, time.time())) if tab == "people" else []
        response = render(request, "sdlc.html", user, project=project, role=role, tab=tab, view=view,
                          work=work, milestones=milestones, releases=releases, comments=comments, members=members,
                          external=external, pending=pending, statuses=STATUSES, kinds=KINDS, priorities=PRIORITIES,
                          selected_status=status, selected_kind=kind, selected_milestone=int(milestone) if milestone else None,
                          internal_roles=sorted(store.INTERNAL_ROLES), external_roles=sorted(store.EXTERNAL_ROLES),
                          now=time.time())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/delivery/{pid}/roles")
    def project_role(request: Request, pid: int, username: str = Form(...), role: str = Form(...)):
        project, user, current = store.project_access(request, pid, need_internal=True)
        store.require_role(current, "lead")
        if role not in store.INTERNAL_ROLES or not db.one(
            """SELECT 1 FROM project_members m JOIN users u USING(username)
               WHERE m.project_id=? AND m.username=? AND u.disabled=0""", (pid, username)
        ):
            raise HTTPException(422, "Choose an enabled project member and a project role")
        if username == project["owner"]:
            raise HTTPException(422, "The project owner is already a lead")
        with db.tx() as conn:
            conn.execute("INSERT INTO sdlc_roles VALUES (?,?,?) ON CONFLICT(project_id,username) DO UPDATE SET role=excluded.role",
                         (pid, username, role))
        audit.log(user["username"], "sdlc.role", str(pid), {"member": username, "role": role}, ip(request))
        return go(pid, "people")

    @app.post("/delivery/{pid}/milestones")
    def add_milestone(request: Request, pid: int, title: str = Form(...), due_on: str = Form("")):
        _, user, role = store.project_access(request, pid, need_internal=True)
        store.require_role(role, "lead")
        if due_on:
            try:
                due_on = date.fromisoformat(due_on).isoformat()
            except ValueError:
                raise HTTPException(422, "Choose a valid due date") from None
        with db.tx() as conn:
            identifier = conn.execute("INSERT INTO sdlc_milestones(project_id,title,due_on) VALUES (?,?,?)",
                                      (pid, text(title, 160), due_on or None)).lastrowid
        audit.log(user["username"], "sdlc.milestone.create", f"{pid}:{identifier}", ip=ip(request))
        return go(pid, "milestones")

    @app.post("/delivery/{pid}/work")
    def add_work(request: Request, pid: int, title: str = Form(...), kind: str = Form(...),
                 description: str = Form(""), priority: str = Form("normal"), owner: str = Form(""),
                 milestone: int = Form(0), related: int = Form(0), reference: str = Form("")):
        project, user, role = store.project_access(request, pid)
        store.require_role(role, "lead", "contributor", "qa")
        if kind not in KINDS or priority not in PRIORITIES:
            raise HTTPException(422, "Choose a valid work type and priority")
        if milestone:
            row("sdlc_milestones", pid, milestone)
        if related and not db.one("SELECT 1 FROM sdlc_work WHERE project_id=? AND number=?", (pid, related)):
            raise HTTPException(422, "Related work item must belong to this project")
        if owner and (user["auth_source"] == "external" or (owner != project["owner"] and not db.one(
            "SELECT 1 FROM project_members WHERE project_id=? AND username=?", (pid, owner)
        ))):
            raise HTTPException(422, "Select an internal project member")
        with db.tx(immediate=True) as conn:
            number = conn.execute("SELECT coalesce(max(number),0)+1 FROM sdlc_work WHERE project_id=?", (pid,)).fetchone()[0]
            conn.execute("INSERT INTO sdlc_work VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (pid, number, kind, text(title, 160), text(description, 8000, False), owner or None,
                          priority, "backlog", milestone or None, related or None, https_url(reference, False), user["username"]))
        audit.log(user["username"], "sdlc.work.create", f"{pid}:{number}", ip=ip(request))
        return go(pid, "work")

    @app.post("/delivery/{pid}/work/{number}/status")
    def change_status(request: Request, pid: int, number: int, status: str = Form(...)):
        _, user, role = store.project_access(request, pid)
        store.require_role(role, "lead", "contributor", "qa")
        if status not in STATUSES:
            raise HTTPException(422, "Choose a valid status")
        with db.tx() as conn:
            result = conn.execute("UPDATE sdlc_work SET status=? WHERE project_id=? AND number=?", (status, pid, number))
            if not result.rowcount:
                raise HTTPException(404)
        audit.log(user["username"], "sdlc.work.status", f"{pid}:{number}", {"status": status}, ip(request))
        return go(pid, "work")

    @app.post("/delivery/{pid}/work/{number}/edit")
    def edit_work(request: Request, pid: int, number: int, title: str = Form(...),
                  description: str = Form(""), priority: str = Form(...), owner: str = Form(""),
                  milestone: int = Form(0), related: int = Form(0), reference: str = Form("")):
        project, user, role = store.project_access(request, pid, need_internal=True)
        store.require_role(role, "lead", "contributor", "qa")
        if priority not in PRIORITIES:
            raise HTTPException(422, "Choose a valid priority")
        if milestone:
            row("sdlc_milestones", pid, milestone)
        if related and (related == number or not db.one(
            "SELECT 1 FROM sdlc_work WHERE project_id=? AND number=?", (pid, related)
        )):
            raise HTTPException(422, "Choose another work item from this project")
        if owner and owner != project["owner"] and not db.one(
            "SELECT 1 FROM project_members WHERE project_id=? AND username=?", (pid, owner)
        ):
            raise HTTPException(422, "Choose a project member")
        with db.tx() as conn:
            updated = conn.execute(
                """UPDATE sdlc_work SET title=?,description=?,priority=?,owner=?,milestone_id=?,
                   related_number=?,reference_url=? WHERE project_id=? AND number=?""",
                (text(title, 160), text(description, 8000, False), priority, owner or None, milestone or None,
                 related or None, https_url(reference, False), pid, number),
            )
            if not updated.rowcount:
                raise HTTPException(404)
        audit.log(user["username"], "sdlc.work.edit", f"{pid}:{number}", ip=ip(request))
        return go(pid, "work")

    @app.post("/delivery/{pid}/work/{number}/comments")
    def comment(request: Request, pid: int, number: int, body: str = Form(...)):
        _, user, role = store.project_access(request, pid)
        store.require_role(role, "lead", "contributor", "commenter", "qa", "release")
        with db.tx() as conn:
            if not db.one("SELECT 1 FROM sdlc_work WHERE project_id=? AND number=?", (pid, number)):
                raise HTTPException(404)
            conn.execute("INSERT INTO sdlc_comments(project_id,work_number,actor,body,created_at) VALUES (?,?,?,?,?)",
                         (pid, number, user["full_name"] if user["auth_source"] == "external" else user["username"],
                          text(body, 4000), time.time()))
        audit.log(user["username"], "sdlc.work.comment", f"{pid}:{number}", ip=ip(request))
        return go(pid, "work")

    @app.post("/delivery/{pid}/releases")
    def add_release(request: Request, pid: int, title: str = Form(...), milestone: int = Form(...),
                    commit: str = Form(...), digest: str = Form(...), ci_url: str = Form(...),
                    evidence: str = Form(...)):
        _, user, role = store.project_access(request, pid, need_internal=True)
        store.require_role(role, "lead", "contributor")
        row("sdlc_milestones", pid, milestone)
        if not re.fullmatch(r"[a-f0-9]{40}", commit) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise HTTPException(422, "Supply the full GitHub commit SHA and the immutable image digest")
        ci_url = https_url(ci_url)
        if urlsplit(ci_url).hostname != "github.com" or not re.fullmatch(r"/[^/]+/[^/]+/actions/runs/[0-9]+/?", urlsplit(ci_url).path):
            raise HTTPException(422, "Link to a GitHub Actions run")
        with db.tx() as conn:
            identifier = conn.execute(
                """INSERT INTO sdlc_releases(project_id,milestone_id,title,commit_sha,digest,ci_url,evidence_url,created_by,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (pid, milestone, text(title, 160), commit, digest, ci_url, https_url(evidence), user["username"], time.time()),
            ).lastrowid
        audit.log(user["username"], "sdlc.release.create", f"{pid}:{identifier}", {"commit": commit, "digest": digest}, ip(request))
        return go(pid, "releases")

    @app.post("/delivery/{pid}/releases/{identifier}/review")
    def review_release(request: Request, pid: int, identifier: int, decision: str = Form(...),
                       note: str = Form(...)):
        _, user, role = store.project_access(request, pid, need_internal=True)
        store.require_role(role, *REVIEW_ROLES.values())
        release = row("sdlc_releases", pid, identifier)
        if release["created_by"] == user["username"]:
            raise HTTPException(403, "Release authors cannot review their own release")
        if decision not in {"approve", "revoke"}:
            raise HTTPException(422, "Choose approve or revoke")
        note = text(note, 2000)
        with db.tx(immediate=True) as conn:
            tests = milestone_tests(release)
            if decision == "approve" and (not tests or any(t["status"] != "done" or not t["reference_url"] for t in tests)):
                raise HTTPException(409, "Complete milestone tests and link their evidence before approving")
            previous = db.one("SELECT 1 FROM sdlc_reviews WHERE release_id=? AND kind<>? AND actor=? AND approved=1",
                              (identifier, role, user["username"]))
            if previous and decision == "approve":
                raise HTTPException(409, "QA and release approvals need distinct reviewers")
            conn.execute(
                """INSERT INTO sdlc_reviews VALUES (?,?,?,?,?,?,?) ON CONFLICT(release_id,kind)
                   DO UPDATE SET actor=excluded.actor, approved=excluded.approved, note=excluded.note,
                   reviewed_at=excluded.reviewed_at, evidence_hash=excluded.evidence_hash""",
                (identifier, role, user["username"], int(decision == "approve"), note, time.time(), evidence_hash(tests)),
            )
        audit.log(user["username"], f"sdlc.release.{decision}", f"{pid}:{identifier}", {"role": role}, ip(request))
        return go(pid, "releases")
