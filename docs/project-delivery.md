# Project delivery pilot

## Enable one project

Create a project and add its internal members through **Projects**. Set `DEVHUB_SDLC_ENABLED=true` and
`SDLC_PILOT_PROJECT_IDS=<project ID>` in the portal deployment environment. Leave the flag off on other
deployments. Open **Project delivery** from the project list. Existing projects and workspace permissions stay
available through their existing pages.

The project owner or hub administrator is a lead. Other internal members start as readers. In **People**, assign
Contributor, QA, or Release to the appropriate named people. QA and release approval require different internal
accounts. Removing a project member removes their delivery role. Users do not need a Coder workspace to participate.

Use **Work** to log requirements, tasks, bugs, and test cases. Link related items, a milestone, and GitHub or test
evidence. The list and board read the same records. Both support form-based status changes without dragging.
Portal audit events record work, role, invitation, and release changes.

## External access

Set up a dedicated HTTPS Keycloak realm and a confidential client used only by this portal's external sign-in.
Provision and verify external email identities in Keycloak. Disable self-registration and require MFA for the client.
Configure Keycloak's assurance mapping to emit the trusted MFA `acr` value in the signed ID token. Register only
`https://<portal-origin>/delivery/external/callback` for the client and require S256 PKCE.

Set `SDLC_EXTERNAL_ISSUER`, `SDLC_EXTERNAL_CLIENT_ID`, and `SDLC_EXTERNAL_MFA_ACR` in the portal environment.
Set `SDLC_EXTERNAL_CLIENT_SECRET` through the deployment secret store. The portal's public URL must use HTTPS.
In **People**, the lead creates a code and shares it privately with the verified recipient. The portal accepts it
once, for seven days, at **External collaborator sign-in**. Returning collaborators sign in without a code.
Project access expires after 30 days unless renewed. Revocation takes effect on the next request. No internal portal
identity, Coder account, or data-access entitlement is created by this flow.

## Mandatory GitHub Actions staging gate

Provision a dedicated Linux Docker runner labelled `devhub-staging` with a staging-only Docker daemon. Register
it with a runner group limited to this repository and trusted deployment workflow. Protect `main` and restrict the
GitHub `staging` environment to that branch. Restrict changes to deployment workflows, scripts, runner
configuration, and the staging Compose file to release operators. Do not store equivalent deployment credentials
in another workflow or on a shared runner. Do not point the staging runner at the production daemon.

Configure the gate portal with `SDLC_STAGING_PROJECT_ID`, `SDLC_GITHUB_REPOSITORY` (exact GitHub spelling),
`SDLC_GITHUB_REPOSITORY_ID` (numeric), and `SDLC_STAGING_IMAGE=ghcr.io/<lowercase-repository>/portal`.
In GitHub's staging environment set `DEVHUB_GATE_URL` to the HTTPS gate portal and
`DEVHUB_STAGING_COMPOSE_FILE` to the staging host's absolute Compose file. Use a gate portal that remains
available during the staging update. The staging Compose file must define `portal.image` as
`${PORTAL_STAGING_IMAGE:?}` and omit `portal.build`. Pre-provision the staging dependencies, an independent
data volume, and a portal health check. The deployment updates only the `portal` service.

1. Run **Build Dev Hub staging artifact** on protected `main`. Copy the immutable digest and Actions run URL
   from the job. Create a Dev Hub release candidate for that commit and digest, with test evidence and a milestone.
2. Complete all milestone test cases with evidence links. Obtain distinct QA and release approvals. Both expire
   after 72 hours. A change to milestone test evidence invalidates existing sign-offs.
3. Run **Deploy Dev Hub staging** on protected `main` with the candidate number and digest. The workflow verifies
   the pulled digest, requests and consumes a two-minute permit, deploys the exact image, and reports its outcome.
   The gate requires a GitHub OIDC token from the configured workflow and runner environment. Keep the default
   subject format `repo:OWNER/REPO:environment:staging`. Each workflow attempt can consume one permit across all
   candidates. Re-running starts a new attempt.
4. To roll back, select a previously successful staging candidate and its digest, renew approvals if necessary,
   and dispatch the same workflow with rollback selected. There is no automatic fallback that bypasses the gate.
   A missing outcome requires checking the actual staging service before retrying.

## Acceptance before expansion

Test with an internal lead, contributor, QA reviewer, release approver, and an external collaborator. Include
keyboard-only and screen-reader participants. Check invitation expiry, MFA, revocation during a session,
cross-project access denial, list and board completion, focus, errors, contrast, and zoom. Ask participants to
report barriers. Do not claim WCAG conformance based only on automated checks.

On staging, exercise a successful deployment and rollback. Verify rejection of altered digests, missing or
expired approvals, self-approval, revoked roles, a different workflow, replay, and a gate outage. Confirm that no
other runner or credential can deploy. Capture delivery time and approval delays. The live Dev Hub and production
deployment remain outside this pilot.
