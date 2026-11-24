"""Extension interfaces + PoC stubs."""
import logging
log = logging.getLogger("extensions")

class LicenseBroker:
    """Windows/commercial licence seat allocation for the 'windows-licensed' network profile.
    Real impl: KMS/ADBA activation, Microsoft VLSC/CSP seat accounting, per-project chargeback."""
    name = "stub (in-memory seat counter)"
    def __init__(self, seats=10): self.seats = seats
    def available(self, used): return max(0, self.seats - used)
    def allocate(self, user, workspace): log.info("licence seat allocated (stub) %s/%s", user, workspace); return True
    def release(self, user, workspace): return True

class KvmProvider:
    """Full desktops / specialist OS on KVM/QEMU via libvirt (Windows, Kali VM, RANGE images).
    Real impl: Coder template using terraform-provider-libvirt (see templates/windows-kvm) on hosts with /dev/kvm."""
    name = "stub (templates/windows-kvm documents the libvirt path)"
    def capabilities(self): return {"kvm_device": "present on PoC host", "status": "template drafted; VM images not provisioned in PoC"}

class ImageScanner:
    """Vulnerability/compliance scanning of workspace images before an image publisher publishes them (e.g. Trivy, Grype)."""
    name = "stub (no scan; always passes)"
    def scan(self, image): return {"image": image, "status": "not-scanned", "critical": None}

class ApprovalWorkflow:
    """Approval for privileged requests (isolated RANGE access, Windows licence, quota increases). Real impl: ServiceNow/Jira/email."""
    name = "stub (auto-approve, audited)"
    def request(self, user, kind, detail): return {"approved": True, "by": "auto (stub)"}

class BillingExporter:
    """FinOps export of usage/cost per project/cost-centre (CSV/ERP/SAP)."""
    name = "stub (CSV export from portal ledger)"

class BackupProvider:
    """Home-volume snapshot/backup (e.g. restic to on-prem object storage, ZFS/Ceph snapshots)."""
    name = "stub (not implemented; volumes persist on the Docker host)"

REGISTRY = {"licence": LicenseBroker(), "kvm": KvmProvider(), "scanner": ImageScanner(),
            "approval": ApprovalWorkflow(), "billing": BillingExporter(), "backup": BackupProvider()}
def get(name): return REGISTRY[name]
