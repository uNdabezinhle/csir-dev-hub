#!/usr/bin/env python3
"""Generates stack/grafana/dashboards/csir-devhub.json (CSIR Dev Hub overview)."""
import json, os
ds = {"type": "prometheus", "uid": "prometheus"}
panels, y = [], 0
def p(title, typ, exprs, w=6, h=5, unit=None, legend=None, x=0):
    global panels
    t = {"type": typ, "title": title, "datasource": ds, "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "targets": [{"refId": chr(65+i), "expr": e, "legendFormat": (legend[i] if legend else "")} for i, e in enumerate(exprs)],
         "fieldConfig": {"defaults": {"unit": unit or "short"}, "overrides": []}, "options": {}}
    if typ == "stat": t["options"] = {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background"}
    panels.append(t)
def row(title):
    global y; panels.append({"type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}}); y += 1
row("Platform health")
p("Coder up", "stat", ['up{job="coder"}'], 4, x=0)
p("Keycloak up", "stat", ['up{job="keycloak"}'], 4, x=4)
p("Portal up", "stat", ['up{job="portal"}'], 4, x=8)
p("Agents connected", "stat", ['sum(coderd_agents_up) or vector(0)'], 4, x=12)
p("Running workspaces", "stat", ['sum(coderd_api_workspace_latest_build{status="succeeded"}) or vector(0)'], 4, x=16)
p("Active users (1h)", "stat", ['max(coderd_api_active_users_duration_hour) or vector(0)'], 4, x=20); y += 5
row("Workspace status")
p("Workspaces by latest build status", "timeseries", ['sum by (status) (coderd_api_workspace_latest_build)'], 12, 8, legend=["{{status}}"], x=0)
p("Workspace builds (rate)", "timeseries", ['sum by (status, workspace_transition) (rate(coderd_workspace_builds_total[5m]))'], 12, 8, legend=["{{workspace_transition}} {{status}}"], x=12); y += 8
row("Host resources")
p("Host CPU %", "timeseries", ['100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[2m])) * 100)'], 8, 8, "percent", x=0)
p("Host memory used", "timeseries", ['node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes', 'node_memory_MemTotal_bytes'], 8, 8, "bytes", ["used", "total"], x=8)
p("Root disk free", "gauge", ['100 * node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}'], 8, 8, "percent", x=16); y += 8
row("Workspace containers (cAdvisor)")
p("Workspace CPU (cores)", "timeseries", ['sum by (name) (rate(container_cpu_usage_seconds_total{name=~"coder-.*"}[2m]))'], 12, 8, legend=["{{name}}"], x=0)
p("Workspace memory", "timeseries", ['sum by (name) (container_memory_working_set_bytes{name=~"coder-.*"})'], 12, 8, "bytes", ["{{name}}"], x=12); y += 8
p("Core stack memory", "timeseries", ['sum by (name) (container_memory_working_set_bytes{name=~"csir-devhub-.*"})'], 24, 8, "bytes", ["{{name}}"], x=0); y += 8
dash = {"uid": "csir-devhub", "title": "CSIR Dev Hub - Platform Overview", "tags": ["csir", "devhub"], "timezone": "browser",
        "schemaVersion": 39, "refresh": "30s", "time": {"from": "now-1h", "to": "now"}, "panels": panels}
out = os.path.join(os.path.dirname(__file__), "..", "stack/grafana/dashboards/csir-devhub.json")
json.dump(dash, open(out, "w"), indent=1); print("wrote", out)
