"""FinOps / billing export (annex: chargeback per user, project / cost centre and workspace).

Usage is reconstructed for a period from three sources:
  * Coder workspace build history -> running intervals (start build completed .. next stop/delete build), so
    allocated vCPU-hours and GB-hours = running hours x the workspace's allocation (portal ledger / build parameters);
  * Prometheus (cAdvisor) -> actually consumed CPU core-hours and average working-set memory per workspace container;
  * the licence broker ledger -> licence seat-days.
Rates come from policy.json (finops.billing): either derived from the active hardware tier (chassis monthly cost split
across vCPU and RAM) or fixed per-unit rates, plus storage per GB-month and the licence pool's cost per seat-month.
Export as JSON or CSV grouped by workspace, user or project; the same data drives Admin -> Billing."""
import csv, io, time, datetime as dt, logging, httpx
from .. import db, coder, quotas
from ..config import POLICY
log = logging.getLogger("billing")
PROM = "http://prometheus:9090"

def _ts(s):
    try: return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None
    except Exception: return None

def bcfg(): return {**POLICY["finops"].get("billing", {}), **(db.get_setting("billing", {}) or {})}

class BillingExporter:
    name = "usage export (Coder build history + Prometheus/cAdvisor + licence ledger) -> CSV/JSON"

    def rates(self, tier_id=None):
        b = bcfg(); cur = POLICY["finops"]["currency"]
        if b.get("rate_source", "tier") == "fixed":
            f = b["fixed_rates"]; r = {"source": "fixed", "per_vcpu_hour": f["per_vcpu_hour"], "per_gb_hour": f["per_gb_hour"]}
        else:
            u = quotas.unit_rates(tier_id); r = {"source": f"tier {u['tier']}", "per_vcpu_hour": u["per_vcpu_hour"], "per_gb_hour": u["per_gb_hour"]}
        r.update(per_storage_gb_month=b.get("per_storage_gb_month", 0.0), default_disk_gb=b.get("default_disk_gb", 20), currency=cur)
        return r

    # ---------------------------------------------------------------- sources
    def running_intervals(self, ws_id):
        """[(start_ts, end_ts|None)] from Coder build history (oldest first)."""
        try: builds = coder.req("GET", f"/api/v2/workspaces/{ws_id}/builds", params={"limit": 200}) or []
        except Exception as e: log.info("builds %s: %s", ws_id, e); return []
        builds = sorted(builds, key=lambda b: b.get("build_number", 0)); out = []; cur = None
        for b in builds:
            job = b.get("job", {}) or {}
            if b["transition"] == "start" and job.get("status") == "succeeded":
                if cur is None: cur = _ts(job.get("completed_at")) or _ts(b["created_at"])
            elif b["transition"] in ("stop", "delete") and cur is not None:
                out.append((cur, _ts(b["created_at"]))); cur = None
        if cur is not None: out.append((cur, None))
        return out

    def prom(self, query, at):
        try:
            r = httpx.get(f"{PROM}/api/v1/query", params={"query": query, "time": at}, timeout=10).json()
            return {m["metric"].get("name", ""): float(m["value"][1]) for m in r["data"]["result"]}
        except Exception as e: log.info("prometheus: %s", e); return None

    # ---------------------------------------------------------------- usage
    def usage(self, start, end, tier_id=None):
        now = time.time(); end = min(end, now); rate = self.rates(tier_id); span = max(1, int(end - start))
        lic_cost = {p["id"]: p["cost_per_seat_month"] for p in db.q("SELECT id, cost_per_seat_month FROM licence_pools")} if self._has("licence_pools") else {}
        projects = {p["id"]: p for p in db.q("SELECT * FROM projects")}
        led = {r["id"]: r for r in db.q("SELECT * FROM workspaces WHERE deleted_at IS NULL OR deleted_at >= ?", (start,))}
        try: live = {w["id"]: w for w in coder.workspaces()}
        except Exception: live = {}
        ids = set(led) | set(live)
        cpu_used = self.prom(f'sum by (name) (increase(container_cpu_usage_seconds_total{{name=~"coder-.+"}}[{span}s]))', end)
        mem_avg = self.prom(f'max by (name) (avg_over_time(container_memory_working_set_bytes{{name=~"coder-.+"}}[{span}s]))', end)
        rows = []
        for wid in ids:
            l = led.get(wid) or {}; w = live.get(wid) or {}
            owner = l.get("owner") or w.get("owner_name"); name = l.get("name") or w.get("name")
            cpu = l.get("cpu") or 0; mem = l.get("mem_gb") or 0
            hrs = 0.0
            for a, b in self.running_intervals(wid):
                a2, b2 = max(a, start), min(b or end, end)
                if b2 > a2: hrs += (b2 - a2) / 3600
            exists_from = max(start, l.get("created_at") or start); exists_to = min(end, l.get("deleted_at") or end)
            days_exist = max(0.0, (exists_to - exists_from) / 86400)
            seat_days = 0.0; lcost = 0.0
            if lic_cost:
                for s in db.q("SELECT * FROM licence_seats WHERE workspace_id=?", (wid,)):
                    a2, b2 = max(s["checked_out_at"], start), min(s["returned_at"] or end, end)
                    if b2 > a2: d = (b2 - a2) / 86400; seat_days += d; lcost += d * lic_cost.get(s["pool"], 0) / 30
            cname = f"coder-{owner}-{name}".lower()
            p = projects.get(l.get("project_id")) or {}
            c_cpu = hrs * cpu * rate["per_vcpu_hour"]; c_mem = hrs * mem * rate["per_gb_hour"]
            c_sto = days_exist / 30 * rate["default_disk_gb"] * rate["per_storage_gb_month"]
            rows.append({"workspace_id": wid, "workspace": f"{owner}/{name}", "owner": owner, "template": l.get("template") or w.get("template_name"),
                "project": p.get("name") or "", "cost_centre": p.get("cost_centre") or "", "profile": l.get("profile") or "",
                "vcpu": cpu, "mem_gb": mem, "running_hours": round(hrs, 3), "vcpu_hours": round(hrs * cpu, 3), "gb_hours": round(hrs * mem, 3),
                "cpu_core_hours_used": round(cpu_used.get(cname, 0) / 3600, 4) if cpu_used is not None else None,
                "mem_gb_avg_used": round(mem_avg.get(cname, 0) / 2**30, 3) if mem_avg is not None else None,
                "licence_seat_days": round(seat_days, 3), "cost_compute": round(c_cpu + c_mem, 2), "cost_storage": round(c_sto, 2),
                "cost_licence": round(lcost, 2), "cost_total": round(c_cpu + c_mem + c_sto + lcost, 2), "currency": rate["currency"],
                "deleted": bool(l.get("deleted_at")) or (wid not in live)})
        return sorted(rows, key=lambda r: -r["cost_total"]), rate, cpu_used is not None

    def _has(self, table): return bool(db.one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)))

    def grouped(self, rows, by):
        if by == "workspace": return rows
        key = {"user": "owner", "project": "project"}[by]; out = {}
        num = ["running_hours", "vcpu_hours", "gb_hours", "cpu_core_hours_used", "licence_seat_days", "cost_compute", "cost_storage", "cost_licence", "cost_total"]
        for r in rows:
            k = r[key] or ("(no project)" if by == "project" else "-")
            g = out.setdefault(k, {by: k, "cost_centre": r["cost_centre"] if by == "project" else "", "workspaces": 0, **{n: 0.0 for n in num}, "currency": r["currency"]})
            g["workspaces"] += 1
            for n in num: g[n] = round(g[n] + (r[n] or 0), 4)
        return sorted(out.values(), key=lambda g: -g["cost_total"])

    def export(self, rows, fmt, meta):
        if fmt == "csv":
            buf = io.StringIO()
            if rows:
                w = csv.DictWriter(buf, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
            return buf.getvalue()
        return {"meta": meta, "rows": rows}
