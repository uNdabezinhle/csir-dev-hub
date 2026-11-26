# Reproducible research project pilot

Project owners can save a blueprint in **Projects → Blueprints and requests**. A blueprint records a source repository
and full Git commit, the active Coder template version at publication, network profile, resources and the local catalog
image ID. Blueprints are immutable. Publish a new name when changing the recipe. Republish the portal templates with
`devhub_request_id` before creating blueprints.

A project member can open a shared blueprint link and launch a workspace using their own portal account. New launches
check the current catalog, image policy, resource quotas and project membership. A changed or missing catalog image
blocks the launch. The portal requests the pinned Coder template version. A colleague still needs separate repository
access and must check out the recorded commit inside the workspace.

Provisioning records are available from the blueprint after Coder confirms workspace creation. They persist after
workspace deletion and capture the recipe, request ID, workspace ID, estimated cost, and Coder build metadata when
returned. The record verifies what the portal requested and observed. Runtime image attestation, automatic source
checkout, and dataset provenance are outside this first release.

## Maintainer pilot

Use the CSIR Dev Hub maintainers as the first pilot team, this repository as the source, and **Ubuntu Server
24.04** with 2 vCPU and 2 GB RAM as the workspace template. Create a private portal project called
`devhub-maintainer-pilot`, with one owner, two distinct maintainer accounts and a separate non-member for the access
check. Allow two concurrent workspaces in the project quota. Use the `dev` network profile if the verification
workspace needs it to reach the approved source repository.

The [versioned fixture](../pilots/research-usage/workspace-usage-v1.csv) contains six fictional observations.
Each row records a project ID, a UTC observation timestamp, and allocated vCPU-hours and GiB-hours. It contains no
real usage or identities. Include the fixture, script, and expected output in the reviewed pilot commit and make that
commit available to both maintainers through their own repository accounts.

### Participant task

1. Open the shared blueprint link, start a timer, and launch your own workspace. Record the first time you can open
   its terminal. Use a unique name such as `maintainer-a-usage` or `maintainer-b-usage`.
2. In the workspace, clone the repository using your own access, check out the exact commit shown on the blueprint,
   and confirm it with `git rev-parse HEAD`.
3. Run `python3 pilots/research-usage/summarize_usage.py --expected-commit FULL_BLUEPRINT_COMMIT > execution-record.json`
   from the checkout. Replace `FULL_BLUEPRINT_COMMIT` with the commit copied from the blueprint. The command uses
   Python 3 and Git. It compares the CSV, baseline, and summarizer against the commit and exits nonzero if they differ.
   A passing run reports **12.00 vCPU-hours** and **25.00 memory GiB-hours** across three fictional projects.
   Compare the per-project values with the [expected summary](../pilots/research-usage/expected-summary.json).
4. Record time to terminal, time to verified report, manual handoffs, and whether another participant had to help.
   Open **Provisioning and execution records** on the shared blueprint and attach your JSON to your own workspace.
   The portal checks the commit against the provisioning record and recalculates the summary hash. The comparison
   shows a match when records from at least two workspace owners agree on the commit, input hashes, and result.
   Each workspace accepts one record of up to 32 KiB. Uploading the same record again is safe.
   Input hashes normalize line endings so Windows and Linux checkouts can be compared.
   The execution record is locally generated and unsigned. It does not attest to the runtime image or mount access.
   Keep the workspace setup and analysis steps independent between participants.

### Pilot acceptance

1. Assign the named pilot owner and confirm the two maintainers, non-member account, and reviewed commit. Take the
   verification stack through the [upgrade and acceptance runbook](upgrade-acceptance.md), including template republication.
2. Measure the current time from request to usable environment and count manual handoffs. Repeat using a blueprint.
   Have a second authorised researcher recreate the environment without the first researcher's credentials.
3. Export the record before and after ordinary workspace deletion. Check that another project cannot read or launch
   the recipe, and that a changed image blocks a launch instead of substituting it.
4. Track time to usable environment, independent reproduction success and handoffs. Record differences in host
   dependencies and any manual source or dataset steps.

## Dataset and compute readiness

Members can submit dataset, specialised compute, and support requests from the project. Hub admins review the intake
from the project's request list and record an outcome. That workflow tracks requests and responses. It does not
grant access or reserve hardware.

| Capability | Required to activate | Acceptance |
|---|---|---|
| Governed dataset access | One approved dataset, a named data steward, an access policy, a versioning scheme, and an approved storage connector | Prove read-only access under the correct user identity, access expiry or revocation, and a version recorded in evidence. |
| GPU and specialised VM capacity | A named infrastructure owner, a GPU/VM pool, resource inventory, and approved scheduling and chargeback policy | Prove a reservation, queue behavior under contention, an estimated cost, and clean release on expiry. |
| Operational support | A named on-call team and approved alert destination | Verify delivery, acknowledgement, and incident escalation. Use the existing [reconciliation response](upgrade-acceptance.md#reconciliation-alert-response). |
| Identity and recovery | Approved identity environment and configured off-host restic repository | Run identity acceptance and verify recovery of the deployed pilot from the real off-host repository. |
