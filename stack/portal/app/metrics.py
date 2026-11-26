"""Prometheus metrics exposed at /metrics (scraped as job 'portal')."""
from prometheus_client import Counter, Gauge

LOGINS = Counter("devhub_logins_total", "Portal logins", ["method", "outcome"])
WS_ACTIONS = Counter("devhub_workspace_actions_total", "Workspace lifecycle actions via portal", ["action", "template"])
QUOTA_DENIALS = Counter("devhub_quota_denials_total", "Workspace requests denied by quota/policy", ["reason"])
AUDIT_EVENTS = Counter("devhub_audit_events_total", "Audit events written", ["action", "outcome"])
RECLAIMED = Counter("devhub_reclaim_actions_total", "Reclaim actions", ["action"])
USERS = Gauge("devhub_users", "Portal users by auth source", ["source"])
WORKSPACES = Gauge("devhub_workspaces", "Workspaces by status and template", ["status", "template"])
COST_RATE = Gauge("devhub_estimated_cost_per_hour", "Estimated running cost per hour (policy currency)")
ALLOC_CPU = Gauge("devhub_allocated_vcpu", "vCPU allocated to running workspaces")
ALLOC_MEM = Gauge("devhub_allocated_mem_gb", "Memory (GB) allocated to running workspaces")
RECONCILE_FAILURES = Counter("devhub_reconcile_failures_total", "Failed workspace reconciliation runs")
RECONCILE_LAST_SUCCESS = Gauge("devhub_reconcile_last_success_timestamp_seconds", "Unix time of the last successful workspace reconciliation")
RECONCILE_OK = Gauge("devhub_reconcile_ok", "Whether the most recent workspace reconciliation succeeded")
