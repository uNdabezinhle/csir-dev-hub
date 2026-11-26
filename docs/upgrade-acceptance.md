# Dev Hub upgrade and acceptance

Use this runbook for releases that change the portal, the engine broker, or the Coder templates. Schedule a maintenance window for an active hub. Use a verification stack for tests that create or delete workspaces, change licence pools, or update image approvals.

## Prepare the release

1. Record the running Git revision, portal and broker image IDs, and active Coder template versions. Keep the previous images available.
2. Take and verify a backup. Confirm the archive passphrase and, when configured, off-host repository access from a separate machine. See [Backup and restore](backup.md).
3. Check for in-flight requests with `./scripts/dc.sh exec portal python -m app.provisioning list` on releases that support the command. Resolve stranded requests before an upgrade or rollback.
4. Validate the intended overlay with `./scripts/dc.sh config -q`. Confirm Docker daemon access for Coder and the engine broker. The portal needs the shared Unix socket volume.

## Upgrade the portal and templates

```bash
./scripts/dc.sh build portal engine-broker
./scripts/dc.sh up -d engine-broker portal
./scripts/dc.sh ps engine-broker portal
```

With an authenticated Coder CLI, push the templates enabled on that host. The bundled templates with the `devhub_request_id` parameter are `ubuntu-server`, `ubuntu-desktop`, `kali`, `ubuntu-vm`, and `windows-kvm`. For example:

```bash
coder templates push ubuntu-server -d templates/ubuntu-server --yes
```

On Docker Desktop, use the existing PowerShell wrapper to run `scripts/bootstrap-coder.py --push` for the supported templates. Push the VM templates only on a stack configured for libvirt. Check each push result and confirm the new version is active. A successful push does not prove that a workspace builds.

## Verify before sign-off

| Check | Acceptance evidence |
|---|---|
| Portal and broker | Both services are running, the broker is healthy, and Admin → Health reports fresh workspace reconciliation. |
| Authentication | Local login and applicable OIDC/MFA work. Public HTTPS cookies are marked `Secure`. A portal POST without a valid CSRF token returns 403. |
| Provisioning, quota and licences | The Coder build has `devhub_request_id`. A failed delete keeps quota and any seat allocated. A confirmed delete releases them after reconciliation. In an isolated restart drill, a lost create response is reconciled to the original project and seat. |
| Image scanning and secrets | Scan an approved test image and confirm the portal reports its local image ID. Verify that an unavailable image fails the gate and a disposable user secret can be added and removed from a test workspace. |
| RDP and VM features | Run `scripts/test-rdp.py` against a running test desktop. For KVM hosts, run `scripts/test-kvm.sh` and verify power and snapshot operations against a disposable VM. |
| Monitoring and recovery | Confirm Prometheus evaluates `PortalReconciliationStale`. If an alert receiver is configured, verify delivery. For disaster recovery, verify a snapshot fetched from the configured off-host repository. |

Record the revision, deployment profile, test commands, results, skips, and evidence location. The regression suite and isolated broker checks are useful evidence. A deployed-stack sign-off needs the checks above against that release.

## Roll back

Stop new provisioning during rollback. Record pending requests and reconcile them against Coder before moving back to a version that cannot interpret them. Restore the previous revision and use its Compose file and images together so the portal and broker agree on their interfaces. If needed, reactivate the previous Coder template versions. Leave existing workspace builds in place unless their owners have agreed to an update.

Keep the current database and backups until you know whether the previous release can read the database. If a database restore is necessary, stop its consumers and use the approved backup. Restoring a database can lose changes made after that backup, so record the recovery point and follow the component restore procedure.

## Production readiness

The project needs named owners and acceptance evidence for these open items:

| Area | Prerequisite and acceptance evidence |
|---|---|
| Windows on hardware KVM | Working KVM host, approved Windows media and licensing. Build the golden image, provision a Windows workspace, and verify agent registration, restart, RDP and licence release. |
| AD, encrypted LDAP and WebAuthn | Directory owner, staging directory and trusted certificates. Verify encrypted federation and group mappings, then test passkey enrollment, fallback and account recovery with pilot users. |
| Central logging and alert delivery | Approved log destination and alert receiver. Verify access controls, retention, searchable audit events, and delivery to the on-call owner. |
| HA, stable TLS and wildcard apps | Approved topology, DNS and certificates. Exercise failover with an active workspace, verify a fresh login and app access, and measure recovery against agreed targets. |
| Backup recovery and recording retention | Named data owners, approved retention and recovery targets, and an off-host repository. Restore on a replacement verification host and test the recording access and deletion workflow. |

## Reconciliation alert response

Prometheus evaluates `PortalReconciliationStale` when reconciliation fails or its last success is older than five
minutes. The rule fires after the condition persists for two minutes. This stack has no configured Alertmanager
receiver, so check the Prometheus Alerts page and portal Admin → Health until alert routing is installed.

1. Review `./scripts/dc.sh logs --tail 100 portal engine-broker coder`. Check Coder reachability, portal service-token
   access, the portal database volume, and any request IDs in the portal logs.
2. From an authenticated admin session, run reconciliation on demand at `POST /admin/reconcile` with a valid CSRF token.
   Check that the health page reports a fresh success. Record the last success, error, affected reservations, and actions.
3. If the error persists, assign the incident to the service owner. Confirm Coder state before manually releasing quota
   or seats. See [Licence broker](licensing-images-billing.md#licence-broker-windows-licensed-profile).
