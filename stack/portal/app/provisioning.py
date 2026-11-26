"""Durable quota reservations and Coder workspace reconciliation."""
import json
import time

from . import audit, db, quotas

RECOVERY_GRACE_SECONDS = 600


def reserve(user, name, template, profile, cpu, memory, project, request_id=None, evidence=None):
    owner = user["username"]
    with db.tx(immediate=True) as conn:
        if db.one("SELECT 1 FROM workspace_requests WHERE owner=? AND name=?", (owner, name)):
            return ["a provisioning request for this workspace is already pending"]
        violations = quotas.check(user, cpu, memory, project)
        if violations:
            return violations
        conn.execute(
            """INSERT INTO workspace_requests
               (owner,name,template,project_id,profile,cpu,mem_gb,created_at,request_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (owner, name, template, project["id"] if project else None, profile, cpu, memory, time.time(), request_id),
        )
        if evidence:
            conn.execute("INSERT INTO provisioning_evidence(request_id,blueprint_id,record) VALUES (?,?,?)",
                         (request_id, evidence["blueprint_id"], json.dumps(evidence)))
    return []


def submitted(owner, name, request_id=None):
    with db.tx() as conn:
        updated = conn.execute(
            "UPDATE workspace_requests SET submitted=? WHERE owner=? AND name=? AND request_id IS ? AND submitted=0",
            (time.time(), owner, name, request_id),
        )
        if not updated.rowcount:
            raise RuntimeError("Workspace reservation is no longer active")


def cancel(owner, name, expected_at=None, request_id=None, unsubmitted_only=False):
    with db.tx(immediate=True) as conn:
        row = db.one("SELECT * FROM workspace_requests WHERE owner=? AND name=?", (owner, name))
        if (not row or (expected_at is not None and row["created_at"] != expected_at)
                or (request_id is not None and row["request_id"] != request_id)
                or (unsubmitted_only and row["submitted"])):
            return
        if row["seat_id"]:
            conn.execute(
                """UPDATE licence_seats SET returned_at=?, returned_by=?, return_reason=?
                   WHERE id=? AND returned_at IS NULL""",
                (time.time(), owner, "provisioning failed", row["seat_id"]),
            )
        conn.execute("DELETE FROM workspace_requests WHERE owner=? AND name=?", (owner, name))
        conn.execute("DELETE FROM provisioning_evidence WHERE request_id=? AND workspace_id IS NULL", (row["request_id"],))
    if row["seat_id"]:
        audit.log(owner, "licence.return", f"{owner}/{name}", {"seat": row["seat_id"], "reason": "provisioning failed"})


def pending():
    return db.q("SELECT * FROM workspace_requests")


def recover(requests, workspaces, observed_at, build_params):
    """Resolve reservations that existed before the successful Coder inventory read."""
    by_name = {(ws["owner_name"], ws["name"]): ws for ws in workspaces}
    for row in requests:
        key = row["owner"], row["name"]
        workspace = by_name.get(key)
        if not row["submitted"]:
            if observed_at - row["created_at"] >= RECOVERY_GRACE_SECONDS:
                cancel(*key, expected_at=row["created_at"], unsubmitted_only=True)
            continue
        if workspace and row["request_id"] and workspace["template_name"] == row["template"]:
            params = build_params(workspace["latest_build"]["id"])
            if params.get("devhub_request_id") == row["request_id"]:
                complete(workspace, expected_at=row["created_at"], request_id=row["request_id"])


def complete(workspace, expected_at=None, request_id=None):
    owner, name = workspace["owner_name"], workspace["name"]
    with db.tx(immediate=True) as conn:
        row = db.one("SELECT * FROM workspace_requests WHERE owner=? AND name=? AND submitted>0", (owner, name))
        if (not row or row["template"] != workspace["template_name"]
                or (request_id is not None and row["request_id"] != request_id)
                or (expected_at is not None and row["created_at"] != expected_at)):
            return False
        conn.execute(
            """INSERT INTO workspaces(id,owner,name,template,project_id,profile,cpu,mem_gb,created_by,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET project_id=excluded.project_id, created_by=excluded.created_by,
                 profile=excluded.profile, cpu=excluded.cpu, mem_gb=excluded.mem_gb, deleted_at=NULL""",
            (workspace["id"], owner, name, row["template"], row["project_id"], row["profile"],
             row["cpu"], row["mem_gb"], owner, row["created_at"]),
        )
        if row["seat_id"]:
            conn.execute("UPDATE licence_seats SET workspace_id=? WHERE id=?", (workspace["id"], row["seat_id"]))
        evidence = db.one("SELECT record FROM provisioning_evidence WHERE request_id=?", (row["request_id"],))
        if evidence:
            record = json.loads(evidence["record"])
            build = workspace.get("latest_build") or {}
            record["coder_confirmation"] = {"observed_at": time.time(), "build_id": build.get("id"),
                                            "reported_template_version_id": build.get("template_version_id")}
            conn.execute("UPDATE provisioning_evidence SET workspace_id=?, record=? WHERE request_id=?",
                         (workspace["id"], json.dumps(record), row["request_id"]))
        conn.execute("DELETE FROM workspace_requests WHERE owner=? AND name=?", (owner, name))
    return True


def operator_release(owner, name):
    """Release a stranded reservation after the operator investigates the original request."""
    from . import coder

    row = db.one("SELECT * FROM workspace_requests WHERE owner=? AND name=?", (owner, name))
    if not row or not row["submitted"]:
        raise ValueError("No submitted reservation exists")
    if time.time() - row["submitted"] < RECOVERY_GRACE_SECONDS:
        raise ValueError("The submission is still within the recovery window")
    live = coder.workspaces(owner)
    for workspace in live:
        if workspace["owner_name"] != owner or workspace["name"] != name:
            continue
        params = coder.build_params(workspace["latest_build"]["id"], strict=True)
        observed_id = params.get("devhub_request_id")
        if not row["request_id"] or not observed_id or observed_id == row["request_id"]:
            raise ValueError("Coder has a potentially matching workspace. Investigate it before releasing quota.")
    cancel(owner, name, expected_at=row["created_at"], request_id=row["request_id"])


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Inspect or release stranded workspace reservations")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    release = commands.add_parser("release")
    release.add_argument("owner")
    release.add_argument("name")
    release.add_argument("--confirmed-no-create", action="store_true", required=True)
    args = parser.parse_args()
    if args.command == "list":
        print(json.dumps(pending(), indent=2))
    else:
        try:
            operator_release(args.owner, args.name)
        except ValueError as error:
            parser.error(str(error))
