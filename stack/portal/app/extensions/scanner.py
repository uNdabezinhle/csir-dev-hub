"""Image scanning + approval gate (annex: image publisher workflow).

Scans run Trivy (aquasec/trivy, pinned in policy.json) as a short-lived container through the Docker API against the
local image store; the JSON report is summarised per severity and stored in the portal DB. Image publishers (and
hub-admins) review results and approve or reject a specific image *digest*. Publishing a catalog item whose image has
no approved scan, or whose latest scan exceeds the critical-CVE threshold, is blocked (policy.json -> image_scanning,
overridable at runtime by a hub-admin). A hub-admin may record a time-boxed risk acceptance instead of fixing CVEs."""
import json, time, threading, logging, gzip, base64
from .. import db, audit, engine
from ..config import POLICY
log = logging.getLogger("scanner")
SEVS = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS image_scans (
  id INTEGER PRIMARY KEY AUTOINCREMENT, image TEXT, image_id TEXT, status TEXT, started_at REAL, finished_at REAL,
  requested_by TEXT, scanner TEXT, critical INTEGER, high INTEGER, medium INTEGER, low INTEGER, unknown INTEGER,
  os TEXT, top TEXT, report_gz TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS image_approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, image TEXT, image_id TEXT, scan_id INTEGER, decision TEXT,
  risk_accepted INTEGER DEFAULT 0, note TEXT, decided_by TEXT, decided_at REAL, expires_at REAL);
"""

def policy():
    return {**POLICY.get("image_scanning", {}), **(db.get_setting("image_scanning", {}) or {})}

class ImageScanner:
    name = "Trivy (container via Docker API) + approval gate"

    def init(self):
        with db.tx() as c: c.executescript(SCHEMA)
        with db.tx() as c:   # scans interrupted by a portal restart
            c.execute("UPDATE image_scans SET status='error', error='interrupted (portal restart)', finished_at=? WHERE status='running'", (time.time(),))

    # -------------------------------------------------------------- scanning
    def image_id(self, image):
        try: return engine.call("image-id", image=image)
        except Exception: return None

    def start_scan(self, image, actor, ip=""):
        iid = self.image_id(image)
        if not iid: raise ValueError(f"image '{image}' not found in the local Docker image store")
        with db.tx() as c:
            sid = c.execute("INSERT INTO image_scans(image,image_id,status,started_at,requested_by,scanner) VALUES (?,?,?,?,?,?)",
                            (image, iid, "running", time.time(), actor, engine.TRIVY_IMAGE)).lastrowid
        audit.log(actor, "image.scan", image, {"scan": sid, "image_id": iid[:19]}, ip)
        threading.Thread(target=self._run, args=(sid, image, iid), daemon=True, name=f"scan-{sid}").start()
        return sid

    def _run(self, sid, image, image_id):
        try:
            rep = engine.call("scan", timeout=1600, image_id=image_id)
            self._store(sid, rep)
            r = self.scan(sid=sid)
            audit.log("scanner", "image.scan.result", image, {"scan": sid, **{k: r[k] for k in ("critical", "high", "medium", "low")}})
        except Exception as e:
            log.warning("scan %s failed: %s", sid, e)
            with db.tx() as c: c.execute("UPDATE image_scans SET status='error', error=?, finished_at=? WHERE id=?", (str(e)[:500], time.time(), sid))
            audit.log("scanner", "image.scan.result", image, {"scan": sid, "error": str(e)[:200]}, outcome="error")

    def ingest(self, image, report, actor="ci", image_id=None):
        """Store an externally produced Trivy JSON report (e.g. uploaded from CI)."""
        with db.tx() as c:
            sid = c.execute("INSERT INTO image_scans(image,image_id,status,started_at,requested_by,scanner) VALUES (?,?,?,?,?,?)",
                            (image, image_id or self.image_id(image) or report.get("Metadata", {}).get("ImageID"), "running", time.time(), actor, "trivy (ingested)")).lastrowid
        self._store(sid, report); audit.log(actor, "image.scan.ingest", image, {"scan": sid}); return sid

    def _store(self, sid, rep):
        vulns = [dict(v, Target=r.get("Target")) for r in rep.get("Results", []) or [] for v in (r.get("Vulnerabilities") or [])]
        cnt = {s: 0 for s in SEVS}
        for v in vulns: cnt[v.get("Severity", "UNKNOWN") if v.get("Severity") in cnt else "UNKNOWN"] += 1
        order = {s: i for i, s in enumerate(SEVS)}
        top = sorted(vulns, key=lambda v: (order.get(v.get("Severity"), 9), v.get("VulnerabilityID", "")))[:50]
        top = [{k: v.get(k) for k in ("VulnerabilityID", "PkgName", "InstalledVersion", "FixedVersion", "Severity", "Title", "Target")} for v in top]
        slim = [{k: v.get(k) for k in ("VulnerabilityID", "PkgName", "InstalledVersion", "FixedVersion", "Severity", "Target")} for v in vulns]
        osinfo = rep.get("Metadata", {}).get("OS") or {}
        with db.tx() as c:
            c.execute("""UPDATE image_scans SET status='done', finished_at=?, critical=?, high=?, medium=?, low=?, unknown=?, os=?, top=?, report_gz=? WHERE id=?""",
                      (time.time(), cnt["CRITICAL"], cnt["HIGH"], cnt["MEDIUM"], cnt["LOW"], cnt["UNKNOWN"],
                       f"{osinfo.get('Family', '')} {osinfo.get('Name', '')}".strip(), json.dumps(top),
                       base64.b64encode(gzip.compress(json.dumps(slim).encode())).decode(), sid))

    # -------------------------------------------------------------- queries
    def scan(self, image=None, sid=None):
        """Latest scan for an image (or a scan by id). Compatible with the old stub: returns dict with 'status'."""
        r = db.one("SELECT * FROM image_scans WHERE id=?", (sid,)) if sid else \
            db.one("SELECT * FROM image_scans WHERE image=? ORDER BY id DESC LIMIT 1", (image,))
        if not r: return {"image": image, "status": "not-scanned", "critical": None}
        r = dict(r); r.pop("report_gz", None); r["top"] = json.loads(r["top"] or "[]"); return r

    def report(self, sid):
        r = db.one("SELECT report_gz FROM image_scans WHERE id=?", (sid,))
        return json.loads(gzip.decompress(base64.b64decode(r["report_gz"]))) if r and r["report_gz"] else []

    def scans(self, limit=100):
        return [dict(r, top=None) for r in db.q("SELECT id,image,image_id,status,started_at,finished_at,requested_by,scanner,critical,high,medium,low,unknown,os,error FROM image_scans ORDER BY id DESC LIMIT ?", (limit,))]

    def approval(self, image, image_id=None):
        iid = image_id or self.image_id(image)
        r = db.one("SELECT * FROM image_approvals WHERE image=? AND image_id=? ORDER BY id DESC LIMIT 1", (image, iid))
        if r and r["expires_at"] and r["expires_at"] < time.time(): r = dict(r, decision="expired")
        return r

    # -------------------------------------------------------------- approval + publish gate
    def decide(self, image, decision, user, note="", risk_accept=False, ip=""):
        from .. import rbac
        if decision not in ("approved", "rejected"): raise ValueError("decision must be approved|rejected")
        s = self.scan(image)
        if s["status"] != "done": raise ValueError("image has no completed scan - scan it first")
        if s["image_id"] != self.image_id(image): raise ValueError("image changed since the last scan - re-scan it")
        p = policy(); over = p.get("block_on_critical", True) and (s["critical"] or 0) > p.get("max_critical", 0)
        if decision == "approved" and over:
            if not risk_accept: raise ValueError(f"{s['critical']} critical CVE(s) > threshold {p.get('max_critical', 0)}: fix and rebuild, or a hub-admin records a risk acceptance")
            if not (p.get("allow_risk_acceptance", True) and rbac.can(user, "*")): raise ValueError("only a hub-admin can accept the risk of critical CVEs")
            if not note.strip(): raise ValueError("a risk acceptance needs a justification / ticket reference")
        exp = time.time() + p.get("risk_acceptance_days", 30) * 86400 if (decision == "approved" and over) else None
        with db.tx() as c:
            c.execute("INSERT INTO image_approvals(image,image_id,scan_id,decision,risk_accepted,note,decided_by,decided_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                      (image, s["image_id"], s["id"], decision, 1 if over else 0, note, user["username"], time.time(), exp))
        audit.log(user["username"], f"image.{decision}", image, {"scan": s["id"], "critical": s["critical"], "risk_accepted": over, "note": note}, ip)

    def gate(self, image):
        """Reasons why an image may NOT be published (empty list = OK)."""
        p = policy(); v = []
        if not p.get("enabled", True) or not image: return v
        s = self.scan(image)
        if s["status"] != "done": return [f"image {image} has no completed vulnerability scan"]
        cur = self.image_id(image)
        if not cur: return [f"image {image} could not be verified against the local image store"]
        if s["image_id"] != cur: v.append(f"image {image} was rebuilt after its last scan - re-scan it")
        over = p.get("block_on_critical", True) and (s["critical"] or 0) > p.get("max_critical", 0)
        a = self.approval(image, s["image_id"])
        if p.get("require_approval", True) and not (a and a["decision"] == "approved"):
            v.append(f"image {image} is not approved by an image publisher" + (f" (last decision: {a['decision']})" if a else ""))
        if over and not (a and a["decision"] == "approved" and a["risk_accepted"]):
            v.append(f"image {image} has {s['critical']} critical CVE(s) (threshold {p.get('max_critical', 0)})")
        return v
