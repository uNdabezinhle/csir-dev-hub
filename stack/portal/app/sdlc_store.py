"""Project delivery data and project-scoped authorisation."""
import os
import time

from fastapi import HTTPException

from . import db, rbac

SCHEMA = """
CREATE TABLE IF NOT EXISTS sdlc_roles (
  project_id INTEGER NOT NULL, username TEXT NOT NULL, role TEXT NOT NULL,
  PRIMARY KEY(project_id, username));
CREATE TABLE IF NOT EXISTS sdlc_external (
  project_id INTEGER NOT NULL, subject TEXT NOT NULL, email TEXT NOT NULL, role TEXT NOT NULL,
  expires_at REAL NOT NULL, revoked_at REAL, PRIMARY KEY(project_id, subject));
CREATE TABLE IF NOT EXISTS sdlc_invitations (
  token_hash TEXT PRIMARY KEY, project_id INTEGER NOT NULL, email TEXT NOT NULL, role TEXT NOT NULL,
  expires_at REAL NOT NULL, redeemed_at REAL, revoked_at REAL);
CREATE TABLE IF NOT EXISTS sdlc_milestones (
  id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, title TEXT NOT NULL, due_on TEXT);
CREATE TABLE IF NOT EXISTS sdlc_work (
  project_id INTEGER NOT NULL, number INTEGER NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
  description TEXT NOT NULL, owner TEXT, priority TEXT NOT NULL, status TEXT NOT NULL,
  milestone_id INTEGER, related_number INTEGER, reference_url TEXT, created_by TEXT NOT NULL,
  PRIMARY KEY(project_id, number));
CREATE TABLE IF NOT EXISTS sdlc_comments (
  id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, work_number INTEGER NOT NULL,
  actor TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sdlc_releases (
  id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, milestone_id INTEGER NOT NULL,
  title TEXT NOT NULL, commit_sha TEXT NOT NULL, digest TEXT NOT NULL, ci_url TEXT NOT NULL,
  evidence_url TEXT NOT NULL, created_by TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sdlc_reviews (
  release_id INTEGER NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL, approved INTEGER NOT NULL,
  note TEXT NOT NULL, reviewed_at REAL NOT NULL, evidence_hash TEXT NOT NULL, PRIMARY KEY(release_id, kind));
CREATE TABLE IF NOT EXISTS sdlc_permits (
  token_hash TEXT PRIMARY KEY, release_id INTEGER NOT NULL, run_id TEXT NOT NULL,
  attempt TEXT NOT NULL, expires_at REAL NOT NULL, consumed_at REAL, outcome TEXT, rollback INTEGER NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS sdlc_consumed_run ON sdlc_permits(release_id, run_id, attempt)
  WHERE consumed_at IS NOT NULL;
"""

INTERNAL_ROLES = {"lead", "contributor", "qa", "release", "reader"}
EXTERNAL_ROLES = {"contributor", "commenter", "reader"}


def enabled():
    return os.environ.get("DEVHUB_SDLC_ENABLED") == "true"


def project_enabled(project_id):
    configured = {value.strip() for value in os.getenv("SDLC_PILOT_PROJECT_IDS", "").split(",")}
    return enabled() and str(project_id) in configured


def require_enabled():
    if not enabled():
        raise HTTPException(404)


def internal_role(project, user):
    if not user:
        return None
    username = user["username"]
    if username == project["owner"] or rbac.can(user, "*"):
        return "lead"
    if not db.one("SELECT 1 FROM project_members WHERE project_id=? AND username=?", (project["id"], username)):
        return None
    assigned = db.one("SELECT role FROM sdlc_roles WHERE project_id=? AND username=?", (project["id"], username))
    return assigned["role"] if assigned else "reader"


def external_membership(project_id, subject, now=None):
    if not project_enabled(project_id):
        return None
    return db.one(
        """SELECT * FROM sdlc_external WHERE project_id=? AND subject=?
           AND revoked_at IS NULL AND expires_at>?""",
        (project_id, subject, time.time() if now is None else now),
    )


def project_access(request, project_id, need_internal=None):
    if not project_enabled(project_id):
        raise HTTPException(404)
    project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    if not project:
        raise HTTPException(404)
    from . import auth
    user = auth.current_user(request)
    role = internal_role(project, user)
    if role:
        return project, user, role
    subject = request.session.get("external_sdlc")
    membership = external_membership(project_id, subject) if subject and not need_internal else None
    if membership:
        return project, {"username": "external:" + subject, "full_name": membership["email"],
                         "auth_source": "external", "roles": []}, membership["role"]
    raise HTTPException(404)


def require_role(role, *allowed):
    if role not in allowed:
        raise HTTPException(403)


def init(conn):
    conn.executescript(SCHEMA)
