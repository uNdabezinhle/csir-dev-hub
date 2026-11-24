# Licence broker, image scanning and billing export (portal modules)

All three modules are in `stack/portal/app/extensions/` (`licence.py`, `scanner.py`, `billing.py`). Their routes are in
`stack/portal/app/phase2.py` and their policy is in `stack/portal/config/policy.json`. Every state change is written to the audit log.

## Licence broker (`windows-licensed` profile)
- **Pools** (`licensing.pools`, seeded into the portal DB on first start and resized in Admin → Licences):
  - `windows-kms`: 10 seats of KMS volume licence, activated against `kms.csir.local:1688`. That KMS host is illustrative.
  - `windows-eval`: 2 seats of 180-day evaluation, PoC and lab only. Its seats are returned on **stop** (`release_on_stop`).
- **Which pool:** `profile_pool` maps a network profile to a pool (`windows-licensed` → `windows-kms`). `template_pool`
  maps a template to a pool and takes priority.
- **Lifecycle:**
  - The portal checks out a seat *before* provisioning and returns it if provisioning fails. Once the workspace exists,
    the seat records its id.
  - Start re-checks out a seat if the workspace has none (for example after a release on stop).
  - Delete returns the seat.
  - `reconcile` runs every minute (with the metrics refresh), or on demand at `POST /admin/reconcile`. It returns seats whose workspace no longer
    exists in Coder, for example when the workspace was deleted with the Coder CLI.
- **Caps and denials:**
  - The pool must have a free seat.
  - `role_max_seats` limits seats per person by highest role (engineer 1, owner 2, publisher 2, hub-admin 5, auditor and guest 0).
  - A project cap (`projects.max_licences`) is set in Admin → Licences.
  - Every denial is audited (`licence.denied`).
- **API:** `GET /api/licences`, which needs `licences.view` (hub-admin, image-publisher, auditor).

## Image scanning and approval
- **CI:** `.github/workflows/ci.yml` job `trivy` builds each workspace image and scans it with the pinned
  `aquasec/trivy:0.76.2` container (exit code 0, report-only). It produces a step-summary table and a JSON artifact.
  The job `trivy-config` scans the Dockerfiles, compose files and Terraform files for misconfigurations, also report-only.
- **Portal** (Admin → Images, permission `images.scan`, held by the image-publisher and hub-admin roles):
  - Scan runs the same Trivy container through the Docker API, with a cache volume and a 1 GB memory limit, and stores the summary plus a gzipped report.
  - The publisher can **approve** or **reject** an image digest (`images.approve`).
  - Only a hub-admin can record a **risk acceptance**, which needs a note and expires after 30 days.
- **Publish gate** (`image_scanning`): a catalog item with an `image` can only be published when all of these hold:
  - a scan exists
  - the image has not been rebuilt since (same digest)
  - it is approved, when `require_approval` is set
  - CRITICAL findings are ≤ `max_critical`, when `block_on_critical` is set, unless a valid risk acceptance exists

  Set `enforce_on_create: true` to apply the same gate when a workspace is created. Hub-admins can change all of these
  at runtime. `GET /api/images/gate?image=...` returns the decision and its reasons.
- **Current state:** the PoC images carry the known CVEs of their upstream bases. For example `ubuntu-server:24.04` has
  CRITICAL 10 / HIGH 131, mostly in code-server's bundled Node modules. The PoC catalog items are published under
  audited risk acceptances. Rebuilding the images with updated packages is the fix.

## Billing / FinOps export
- **Sources:**
  - Running intervals come from **Coder** build history (start to stop or delete transitions).
  - Requested vCPU and RAM come from the portal ledger, which also gives licence seat-days.
  - Actual CPU-hours (cAdvisor `container_cpu_usage_seconds_total` increase) and average memory come from **Prometheus**.
- **Rates** (`finops.billing`):
  - `rate_source: tier` derives per-vCPU-hour and per-GB-hour rates from the active hardware tier's monthly cost.
  - `fixed` uses `fixed_rates`.
  - Storage is charged at `per_storage_gb_month`, and licences at the pool's `cost_per_seat_month`.
  - All figures are illustrative, in ZAR.
- **Export:** `GET /api/billing/usage?from=YYYY-MM-DD&to=YYYY-MM-DD&group_by=workspace|user|project&format=json|csv`.
  Admin → Billing shows the same data with download links. Every export is audited.
- **Access:** hub-admins and auditors see everything. A project-owner (`finops.view` + `billing.own_projects`) sees only
  their own projects.
