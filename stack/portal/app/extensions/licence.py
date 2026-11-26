"""Licence broker for the 'windows-licensed' network profile (annex: Windows/commercial licence seat management).

Seat pools (policy.json -> licensing.pools) model KMS/MAK/CSP entitlements. A seat is checked out when a workspace on
a licensed profile is created (or started again after a release-on-stop), and returned when the workspace is deleted
(portal action, reclaim, or reconcile when it disappears from Coder) or, optionally, stopped. Every checkout, denial,
return, resize and manual assignment is written to the audit trail. Limits integrate with quotas: per-role seat caps
(licensing.role_max_seats) and per-project caps (projects.max_licences)."""
import json, time, logging
from .. import db, audit
from ..config import POLICY
log = logging.getLogger("licence")

SCHEMA = """
CREATE TABLE IF NOT EXISTS licence_pools (
  id TEXT PRIMARY KEY, product TEXT, activation TEXT, kms_host TEXT, seats INTEGER NOT NULL,
  cost_per_seat_month REAL DEFAULT 0, release_on_stop INTEGER DEFAULT 0, updated_by TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS licence_seats (
  id INTEGER PRIMARY KEY AUTOINCREMENT, pool TEXT NOT NULL, kind TEXT NOT NULL,           -- workspace | manual
  workspace_id TEXT, workspace TEXT, username TEXT, project_id INTEGER, note TEXT,
  checked_out_at REAL NOT NULL, checked_out_by TEXT, returned_at REAL, returned_by TEXT, return_reason TEXT);
CREATE INDEX IF NOT EXISTS ix_seats_open ON licence_seats(pool, returned_at);
"""

class LicenceDenied(Exception): pass

def cfg(): return POLICY.get("licensing", {})

class LicenseBroker:
    name = "seat-pool broker (SQLite ledger, KMS/MAK pools from policy.json)"

    # ---------------------------------------------------------------- setup
    def init(self):
        with db.tx() as c:
            c.executescript(SCHEMA)
            cols = [r[1] for r in c.execute("PRAGMA table_info(projects)").fetchall()]
            if "max_licences" not in cols: c.execute("ALTER TABLE projects ADD COLUMN max_licences INTEGER")
            for p in cfg().get("pools", []):
                c.execute("""INSERT OR IGNORE INTO licence_pools(id,product,activation,kms_host,seats,cost_per_seat_month,release_on_stop,updated_by,updated_at)
                             VALUES (?,?,?,?,?,?,?,?,?)""", (p["id"], p["product"], p.get("activation", "kms"), p.get("kms_host", ""),
                             int(p["seats"]), float(p.get("cost_per_seat_month", 0)), 1 if p.get("release_on_stop") else 0, "seed", time.time()))

    # ---------------------------------------------------------------- queries
    def pool_for(self, profile, template=None):
        """Which pool (if any) a workspace on this profile/template draws from."""
        t = cfg().get("template_pool", {}).get(template or "")
        return t or cfg().get("profile_pool", {}).get(profile)

    def pools(self):
        out = []
        for p in db.q("SELECT * FROM licence_pools ORDER BY id"):
            used = self.used(p["id"]); out.append({**p, "used": used, "available": max(0, p["seats"] - used)})
        return out

    def pool(self, pid): return db.one("SELECT * FROM licence_pools WHERE id=?", (pid,))
    def used(self, pid): return db.one("SELECT COUNT(*) n FROM licence_seats WHERE pool=? AND returned_at IS NULL", (pid,))["n"]
    def open_seats(self, pool=None):
        sql, a = "SELECT * FROM licence_seats WHERE returned_at IS NULL", []
        if pool: sql += " AND pool=?"; a.append(pool)
        return db.q(sql + " ORDER BY checked_out_at", a)
    def history(self, limit=200): return db.q("SELECT * FROM licence_seats ORDER BY id DESC LIMIT ?", (limit,))
    def seat_for_ws(self, ws_id): return db.one("SELECT * FROM licence_seats WHERE workspace_id=? AND returned_at IS NULL", (ws_id,))
    def user_seats(self, username): return db.one("SELECT COUNT(*) n FROM licence_seats WHERE username=? AND returned_at IS NULL", (username,))["n"]

    def role_limit(self, roles):
        lim = cfg().get("role_max_seats", {}); vals = [lim[r] for r in roles or [] if r in lim]
        return max(vals) if vals else 0

    # legacy stub interface (kept for callers that only need a yes/no)
    @property
    def seats(self): return sum(p["seats"] for p in self.pools())
    def available(self, used=None): return sum(p["available"] for p in self.pools())

    # ---------------------------------------------------------------- policy check (quota integration)
    def precheck(self, user, pool_id, project=None):
        """Violations that would block a checkout (empty list = allowed)."""
        v = []; p = self.pool(pool_id)
        if not p: return [f"licence pool '{pool_id}' is not configured"]
        if self.used(pool_id) >= p["seats"]: v.append(f"no seats left in licence pool '{pool_id}' ({p['seats']} in use)")
        if user is not None:
            lim = self.role_limit(user.get("roles"))
            if self.user_seats(user["username"]) + 1 > lim: v.append(f"licence seat quota for your role is {lim}")
        if project and project.get("max_licences") is not None:
            n = db.one("SELECT COUNT(*) n FROM licence_seats WHERE project_id=? AND returned_at IS NULL", (project["id"],))["n"]
            if n + 1 > project["max_licences"]: v.append(f"project '{project['name']}' licence quota {project['max_licences']} reached")
        return v

    # ---------------------------------------------------------------- lifecycle
    def checkout(self, pool_id, user, actor, workspace_id=None, workspace="", project=None, kind="workspace", note="", ip="", enforce=True, request_name=None, request_id=None):
        with db.tx():   # serialises concurrent checkouts (db._lock is re-entrant)
            v = self.precheck(user if enforce else None, pool_id, project)
            if v:
                audit.log(actor, "licence.checkout", workspace or note, {"pool": pool_id, "violations": v}, ip, "denied")
                raise LicenceDenied("; ".join(v))
            if workspace_id and self.seat_for_ws(workspace_id): return self.seat_for_ws(workspace_id)
            with db.tx() as c:
                cur = c.execute("""INSERT INTO licence_seats(pool,kind,workspace_id,workspace,username,project_id,note,checked_out_at,checked_out_by)
                                   VALUES (?,?,?,?,?,?,?,?,?)""", (pool_id, kind, workspace_id, workspace, (user or {}).get("username"),
                                   (project or {}).get("id"), note, time.time(), actor))
                sid = cur.lastrowid
                if request_name:
                    updated = c.execute("UPDATE workspace_requests SET seat_id=? WHERE owner=? AND name=? AND request_id IS ?",
                                        (sid, user["username"], request_name, request_id))
                    if not updated.rowcount:
                        raise LicenceDenied("workspace reservation expired")
        audit.log(actor, "licence.checkout", workspace or note, {"pool": pool_id, "seat": sid, "kind": kind,
                  "user": (user or {}).get("username"), "used": self.used(pool_id)}, ip)
        return db.one("SELECT * FROM licence_seats WHERE id=?", (sid,))

    def release(self, actor, reason, seat_id=None, workspace_id=None, ip=""):
        s = db.one("SELECT * FROM licence_seats WHERE id=? AND returned_at IS NULL", (seat_id,)) if seat_id else self.seat_for_ws(workspace_id)
        if not s: return None
        with db.tx() as c:
            c.execute("UPDATE licence_seats SET returned_at=?, returned_by=?, return_reason=? WHERE id=?", (time.time(), actor, reason, s["id"]))
        audit.log(actor, "licence.return", s["workspace"] or s["note"], {"pool": s["pool"], "seat": s["id"], "reason": reason}, ip)
        return s

    def resize(self, pool_id, seats, actor, ip=""):
        if seats < 0: raise ValueError("seats must be >= 0")
        with db.tx() as c: c.execute("UPDATE licence_pools SET seats=?, updated_by=?, updated_at=? WHERE id=?", (seats, actor, time.time(), pool_id))
        audit.log(actor, "licence.pool.resize", pool_id, {"seats": seats, "in_use": self.used(pool_id)}, ip)

    def reconcile(self, live, actor="licence-broker", deleted=None):
        """Tie seats to the Coder workspace lifecycle. live = {ws_id: coder workspace}."""
        from .. import coder
        changed = []
        for s in self.open_seats():
            if s["kind"] != "workspace": continue
            if db.one("SELECT 1 FROM workspace_requests WHERE seat_id=?", (s["id"],)): continue
            ws = live.get(s["workspace_id"])
            if ws is None:
                if deleted is None or s["workspace_id"] in deleted:
                    changed.append(self.release(actor, "workspace deleted", seat_id=s["id"]))
            elif (self.pool(s["pool"]) or {}).get("release_on_stop") and coder.ws_status(ws) == "stopped" and ws["latest_build"]["transition"] == "stop":
                changed.append(self.release(actor, "workspace stopped (release_on_stop)", seat_id=s["id"]))
        return [c for c in changed if c]

    def capabilities(self):
        return {"pools": self.pools(), "role_max_seats": cfg().get("role_max_seats", {})}
