"""Idle reclaim / lifecycle policy (FinOps): stop idle running workspaces, flag (optionally delete) long-stopped ones,
expire guest workspaces. Runs every 10 minutes in the background and on demand (dry-run or apply) from the admin console."""
import time, threading, logging, fnmatch, datetime as dt
from . import coder, db, audit
from .config import POLICY
from .metrics import RECLAIMED
log = logging.getLogger("reclaim")

def policy():
    return {**POLICY["reclaim"], **(db.get_setting("reclaim_policy", {}) or {})}

def _ts(s):
    try: return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception: return None

def evaluate():
    p = policy(); now = time.time(); plan = []
    guests = {u["username"] for u in db.q("SELECT username, roles FROM users") if '"guest"' in u["roles"] and '"engineer"' not in u["roles"]}
    exempt = p.get("exempt", [])
    for ws in coder.workspaces():
        if any(fnmatch.fnmatch(f"{ws['owner_name']}/{ws['name']}", pat) for pat in exempt): continue   # pinned demo/shared workspaces
        st = coder.ws_status(ws); last = _ts(ws.get("last_used_at") or "") or _ts(ws["latest_build"]["created_at"])
        idle_min = (now - last) / 60 if last else 0
        created = _ts(ws.get("created_at", "")) or now
        if st == "running" and idle_min >= p["idle_stop_minutes"]:
            plan.append({"ws": ws, "action": "stop", "reason": f"idle {int(idle_min)} min >= {p['idle_stop_minutes']}"})
        elif st == "stopped" and idle_min >= p["stopped_delete_days"] * 1440:
            plan.append({"ws": ws, "action": "delete" if p["delete_enabled"] else "flag",
                         "reason": f"stopped/unused {idle_min/1440:.1f} d >= {p['stopped_delete_days']} d"})
        if ws["owner_name"] in guests and (now - created) / 86400 >= p["guest_max_age_days"] and st != "deleted":
            plan.append({"ws": ws, "action": "delete" if p["delete_enabled"] else "stop", "reason": f"guest workspace older than {p['guest_max_age_days']} d"})
    return plan

def apply(plan, actor="reclaimer"):
    done = []
    for item in plan:
        ws, act = item["ws"], item["action"]
        try:
            if act in ("stop", "delete"):
                coder.build(ws["id"], act)
            RECLAIMED.labels(action=act).inc()
            audit.log(actor, f"reclaim.{act}", f"{ws['owner_name']}/{ws['name']}", item["reason"])
            done.append(item)
        except Exception as e:
            audit.log(actor, f"reclaim.{act}", f"{ws['owner_name']}/{ws['name']}", str(e), outcome="error")
    return done

def loop():
    while True:
        time.sleep(600)
        try:
            if policy().get("auto_apply"): apply(evaluate())
        except Exception as e: log.warning("reclaim loop: %s", e)

def start():
    threading.Thread(target=loop, daemon=True, name="reclaim").start()
