"""Quotas (per role / per user override / per project) and FinOps cost estimation."""
import json
from . import db
from .config import POLICY

def effective_quota(user):
    """Most generous quota among the user's roles, unless an explicit per-user override exists."""
    ov = db.one("SELECT * FROM user_quotas WHERE username=?", (user["username"],))
    if ov: return {"max_workspaces": ov["max_workspaces"], "max_cpu": ov["max_cpu"], "max_mem_gb": ov["max_mem_gb"], "source": "user override"}
    best = {"max_workspaces": 0, "max_cpu": 0, "max_mem_gb": 0}
    for r in user.get("roles", []):
        rq = POLICY["role_quotas"].get(r)
        if rq:
            for k in best: best[k] = max(best[k], rq[k])
    best["source"] = "role default"
    return best

def ledger(owner=None, project_id=None, include_pending=False):
    sql, args = "SELECT * FROM workspaces WHERE deleted_at IS NULL", []
    if include_pending:
        sql = """SELECT * FROM (
            SELECT owner, project_id, cpu, mem_gb FROM workspaces WHERE deleted_at IS NULL
            UNION ALL SELECT owner, project_id, cpu, mem_gb FROM workspace_requests
        ) WHERE 1=1"""
    if owner: sql += " AND owner=?"; args.append(owner)
    if project_id: sql += " AND project_id=?"; args.append(project_id)
    return db.q(sql, args)

def usage(rows):
    return {"workspaces": len(rows), "cpu": sum(r["cpu"] or 0 for r in rows), "mem_gb": sum(r["mem_gb"] or 0 for r in rows)}

def check(user, cpu, mem_gb, project=None):
    """Returns list of violations (empty = allowed). Allocation counts every non-deleted workspace (stopped ones keep storage/seat)."""
    v = []
    q = effective_quota(user); u = usage(ledger(owner=user["username"], include_pending=True))
    if u["workspaces"] + 1 > q["max_workspaces"]: v.append(f"workspace count {u['workspaces']+1} > quota {q['max_workspaces']}")
    if u["cpu"] + cpu > q["max_cpu"]: v.append(f"vCPU {u['cpu']+cpu} > quota {q['max_cpu']}")
    if u["mem_gb"] + mem_gb > q["max_mem_gb"]: v.append(f"memory {u['mem_gb']+mem_gb} GB > quota {q['max_mem_gb']} GB")
    if project:
        pu = usage(ledger(project_id=project["id"], include_pending=True))
        if project["max_workspaces"] and pu["workspaces"] + 1 > project["max_workspaces"]: v.append(f"project '{project['name']}' workspace quota {project['max_workspaces']} reached")
        if project["max_cpu"] and pu["cpu"] + cpu > project["max_cpu"]: v.append(f"project '{project['name']}' vCPU quota {project['max_cpu']} exceeded")
        if project["max_mem_gb"] and pu["mem_gb"] + mem_gb > project["max_mem_gb"]: v.append(f"project '{project['name']}' memory quota {project['max_mem_gb']} GB exceeded")
    return v

# ---------------------------------------------------------------- FinOps
def tier(tier_id=None):
    f = POLICY["finops"]; tid = tier_id or db.get_setting("finops_tier", f["active_tier"])
    return tid, f["tiers"][tid]

def unit_rates(tier_id=None):
    """Split the chassis monthly cost 50/50 across vCPU and RAM, per hour (730 h/month)."""
    tid, t = tier(tier_id)
    return {"tier": tid, "per_vcpu_hour": t["monthly_cost"] * 0.5 / t["vcpu"] / 730,
            "per_gb_hour": t["monthly_cost"] * 0.5 / t["ram_gb"] / 730, "currency": POLICY["finops"]["currency"]}

def estimate(cpu, mem_gb, tier_id=None):
    r = unit_rates(tier_id); hourly = cpu * r["per_vcpu_hour"] + mem_gb * r["per_gb_hour"]
    return {"hourly": round(hourly, 2), "monthly": round(hourly * POLICY["finops"]["hours_per_month_with_autostop"], 2),
            "monthly_always_on": round(hourly * 730, 2), "currency": r["currency"]}

def capacity(tier_id=None):
    """Headroom of the selected hardware tier vs. current allocations (all non-deleted workspaces)."""
    tid, t = tier(tier_id); u = usage(ledger())
    return {"tier": tid, **t, "alloc_cpu": u["cpu"], "alloc_mem": u["mem_gb"], "workspaces": u["workspaces"],
            "cpu_pct": round(100 * u["cpu"] / t["vcpu"], 1), "mem_pct": round(100 * u["mem_gb"] / t["ram_gb"], 1),
            "per_user_vcpu": round(t["vcpu"] / t["users"], 1), "per_user_ram": round(t["ram_gb"] / t["users"], 1)}
