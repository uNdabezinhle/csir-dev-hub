# Sizing per hardware tier (~30 / 40 / 60 / 70 users)

The default chassis is the **~70-user** tier (`finops.active_tier = "70"` in `stack/portal/config/policy.json`). These
figures are planning assumptions. Replace them with the Technical Annex BoM and costing; the portal's FinOps page
reads them from policy.json. **ZAR costs in policy.json are illustrative placeholders.**

## Assumptions
- Concurrency: 60–70% of named users active at peak. Autostop (idle reclaim after 120 min) keeps the average down.
- Typical workspace: 2 vCPU / 4 GB (engineer). Heavy builds: 4 vCPU / 8 GB. Desktop: +1 GB. Windows VM: 4 vCPU / 8 GB / 64 GB.
- CPU overcommit 2:1 for containers (they are bursty). No RAM overcommit (Docker `memory` is a hard limit).
- Control plane reserve: ~16 vCPU / 48 GB for Coder, Postgres, Keycloak, portal, Guacamole, Prometheus/Grafana and Loki.

## Tiers

| Tier | Named users | Peak concurrent | vCPU (physical threads) | RAM | Storage (usable) | Windows VM seats | Suggested layout |
|---|---|---|---|---|---|---|---|
| 30 | 30 | ~20 | 96 | 384 GB | 8 TB | 4–6 | 1 control VM + 2 compute nodes |
| 40 | 40 | ~28 | 128 | 512 GB | 12 TB | 6–8 | 1 control VM + 2–3 compute nodes |
| 60 | 60 | ~42 | 192 | 768 GB | 16 TB | 8–10 | HA control (3) + 3 compute nodes |
| **70 (default)** | 70 | ~49 | 256 | 1 TB | 20 TB | 10 | HA control (3) + 4 compute nodes (2 Docker, 2 KVM) |

Rough check for the 70 tier:
- 49 active × (2 vCPU / 4 GB) gives ~98 vCPU and ~196 GB.
- 10 Windows VMs × (4 vCPU / 8 GB) gives 40 vCPU and 80 GB.
- Add the control plane, ~16 vCPU / 48 GB.
- Total: ~154 vCPU (60% of 256) and ~324 GB (~32% of 1 TB).

That leaves headroom for heavy builds, Kali ranges and growth. Storage: 70 × 50 GB home volumes (3.5 TB), plus images
and golden VMs (~2 TB), plus Windows data disks (10 × 64 GB), plus backups and recordings.

## Per-role workspace quotas (enforced by the portal)

| Role | Max workspaces | Max vCPU | Max RAM (GB) |
|---|---|---|---|
| hub-admin | 10 | 16 | 32 |
| image-publisher | 4 | 8 | 16 |
| project-owner | 4 | 8 | 16 |
| engineer | 3 | 6 | 12 |
| auditor | 1 | 2 | 4 |
| guest | 1 | 1 | 2 |

How quotas are worked out:
- The effective quota is the most generous role a user holds, unless an admin override exists (admin → Quotas).
- Project quotas also apply.
- Per-workspace limits are set by template parameters: cpu 1–4 and memory 2–16 GB for ubuntu-server.
- Requests over the limit are denied and audited (`workspace.create … denied`).

## Per-workspace limits (template level)
- `memory` is a hard limit (OOM-kill beyond it). `cpu_shares` sets the weight.
- `cpu_set` pins to cores `0..n-1`. On the PoC that means co-located workspaces share the same cores. For
  production, use a CPU quota (`--cpus`) or k8s requests/limits, or let a placement helper spread `cpu_set`.
- Home volume: persistent named volume `coder-<wsid>-home`. Set a size with an XFS project quota or an LVM thin
  volume per workspace (planned).
- `shm_size`: 512 MB for desktop images.

## The PoC box
8 vCPU and ~15 GB RAM. The core stack uses ~5–6 GB. Up to about 3 small workspaces run at once
(today: demo-ubuntu, demo-desktop, demo-kali). That's fine for a demonstration but not for load testing.
