"""Immutable, project-scoped launch recipes and evidence for portal-managed workspaces."""
import json
import re
import time
from urllib.parse import urlsplit

from . import db


def parse_source(repository, revision):
    repository, revision = repository.strip(), revision.strip().lower()
    try:
        url = urlsplit(repository)
    except ValueError as error:
        raise ValueError("Enter a valid HTTPS repository URL") from error
    if (url.scheme != "https" or not url.hostname or url.username is not None or url.password is not None
            or url.query or url.fragment or len(repository) > 2048 or "\\" in repository
            or not url.path.strip("/") or any(ch.isspace() for ch in repository)):
        raise ValueError("Use an HTTPS repository URL without credentials, a query or a fragment")
    if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", revision):
        raise ValueError("Enter a full Git commit ID")
    return repository, revision


def add(project_id, name, spec, actor):
    name = name.strip()
    if not 1 <= len(name) <= 80:
        raise ValueError("Blueprint name must be between 1 and 80 characters")
    with db.tx() as conn:
        return conn.execute(
            "INSERT INTO project_blueprints(project_id,name,spec,created_by,created_at) VALUES (?,?,?,?,?)",
            (project_id, name, json.dumps(spec), actor, time.time()),
        ).lastrowid


def for_project(project_id):
    rows = db.q("SELECT * FROM project_blueprints WHERE project_id=? ORDER BY id DESC", (project_id,))
    return [{**row, "spec": json.loads(row["spec"])} for row in rows]


def get(blueprint_id):
    row = db.one("SELECT * FROM project_blueprints WHERE id=?", (blueprint_id,))
    return {**row, "spec": json.loads(row["spec"])} if row else None


def records(blueprint_id):
    return db.q(
        """SELECT e.workspace_id, w.name, w.owner, w.created_at
           FROM provisioning_evidence e JOIN workspaces w ON w.id=e.workspace_id
           WHERE e.blueprint_id=? ORDER BY w.created_at DESC""",
        (blueprint_id,),
    )
