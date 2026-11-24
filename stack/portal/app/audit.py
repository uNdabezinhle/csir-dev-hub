"""Append-only audit trail (who did what, to what, outcome). Viewable by hub-admins and auditors; CSV export."""
import time, json
from . import db
from .metrics import AUDIT_EVENTS

def log(actor, action, target="", detail=None, ip="", outcome="ok"):
    with db.tx() as c:
        c.execute("INSERT INTO audit(ts, actor, action, target, detail, ip, outcome) VALUES (?,?,?,?,?,?,?)",
                  (time.time(), actor or "-", action, target, json.dumps(detail) if isinstance(detail, (dict, list)) else (detail or ""), ip, outcome))
    AUDIT_EVENTS.labels(action=action, outcome=outcome).inc()

def recent(limit=200, actor=None, action=None):
    sql, args = "SELECT * FROM audit WHERE 1=1", []
    if actor: sql += " AND actor=?"; args.append(actor)
    if action: sql += " AND action LIKE ?"; args.append(action + "%")
    sql += " ORDER BY id DESC LIMIT ?"; args.append(limit)
    return db.q(sql, args)
