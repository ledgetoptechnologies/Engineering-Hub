# Manual staging session: evidence-to-field worksheet

Status: preparation worksheet only. It does not authorize staging, change a session grant, select a project, start a service, or assert any report value. Use it for `engineering-hub.manual-staging-session.v2` as described in [Trusted manual staging startup](trusted-manual-staging-startup.md). Retained v1 files can be read but cannot activate a manual session. Normal Hub startup and Compose remain unchanged.

This worksheet maps the current source schema and [live operator gates](live-staging-operator-gates.md) to the evidence an operator would need before constructing a fresh manual-session report. Keep completed evidence in the approved protected record location; do not place credentials, bearer tokens, private keys, or a ready-to-activate enrollment JSON in this repository.

## Fixed bindings

| Existing field | Required binding | Evidence to retain | Collection / authority boundary |
|---|---|---|---|
| `schema` | Exact literal `engineering-hub.manual-staging-session.v2` | Schema/version reference only | Data-format check; conveys no approval. |
| `release.issuer_id` | Independently enrolled executor issuer | Enrollment record and source path/version | Read the retained enrollment. Never choose an issuer based on the executor response. |
| `release.public_key_hex` | Enrolled Ed25519 public key, 64 lowercase hex characters | Enrollment record fingerprint and independent key comparison | Public key material is not a credential, but keep the full enrollment protected as documented. |
| `release.implementation_commit` | Exact 40-character source commit | Retained release enrollment and immutable artifact metadata | Compare to the enrolled release; do not infer from a mutable tag. |
| `release.runnable_digest`, `release.platform_manifest_digest`, `release.image_config_id`, `release.launch_profile_digest` | Exact enrolled `sha256:<64 lowercase hex>` pins | Release enrollment and immutable image/manifest evidence | Read-only image/manifest inspection can collect identity; it does not prove the image is the running executor. |
| `release.platform` | Exact closed platform object | Enrolled platform record and runtime platform observation | Must match the enrollment/profile; no platform substitution. |
| `launch.launch_id`, `launch.boot_id` | Exact enrolled UUIDs | Original launch receipt and current process/boot source | Compare against the retained receipt and current authenticated supervisor/host evidence. A new process challenge does not renew these IDs. |
| `launch.authenticated_host_pid`, `launch.process_start_ticks`, `launch.container_id` | Exact positive process pins and container ID | Original launch receipt plus current host/supervisor and Engine observation | A container list alone is not proof that the recorded process is the current one. Reconciliation needs the authoritative host sources and their provenance. |
| `launch.engine_id`, `launch.engine_pid`, `launch.engine_start_ticks` | Exact enrolled Engine and process identity | Original launch receipt plus current Engine peer/process observation | Read-only Engine inspection is possible only through an already authorized, identified endpoint. Do not start or adopt an Engine to collect evidence. |
| `launch.approval_expires_at_ms` | Original approval expiry | Original launch receipt | Preserve the original value; report expiry must not exceed it. |
| `operator_actor` | Exact independently enrolled `human:cf:<64 lowercase hex>` operator | Existing operator enrollment/identity record | This is not the first visitor, global allowlist, an agent identity, or an operator choice supplied by the request. No credential or session cookie belongs in the worksheet. |
| `scope.actors`, `scope.task_ids` | Nonempty, unique and lexicographically sorted arrays of allowed agent names and canonical lowercase task UUIDs | Independently reviewed session scope, within the protected selector's existing actor/task ceiling | The exact scope appears in the challenge evidence. This is a veto, not task execution approval; omitted held work stays reserved. |

The `executor` object under `report` is closed: it must exactly repeat every `launch` field above plus `image_config_id` from the enrolled release, with exact types. Do not mint fresh identities or rebind a report by editing enrollment.

## Report fields and evidentiary meaning

| Existing report field | Required content | Evidence source and limit |
|---|---|---|
| `target_id` | Exact `engineering-hub:staging` | Fixed target identity; does not establish that the target is safe or current. |
| `manifest_digest` | Exact enrolled manifest digest | Independently retained manifest bytes/digest and reviewed task binding; a digest alone is not task assignment or independent review. |
| `machine_id` | Exact `staging-pc` | Enrolled manifest/profile and current identified machine source. |
| `source` | Bounded identifier for the evidence source | Record who/what collected it and the source revision or observation ID; this field is provenance metadata, not proof. |
| `observed_at_ms`, `expires_at_ms` | Original observation time and short expiry | Use the original collection times. Source enforces not-future, no more than five minutes old at construction, at most ten-minute report lifetime, and expiry no later than the launch approval expiry. A new session challenge does not refresh these facts. |
| `evidence` | 1â€“6000 UTF-8 bytes of bounded, non-secret evidence summary | Summarize the observations and their provenance; preserve complete evidence separately in the protected record. Do not include tokens, private keys, credential contents, or unrelated client data. |
| `evidence_sha256` | Exact digest of the UTF-8 evidence bytes | Integrity check for the supplied text only. It does not validate the truth of the evidence. |
| `attestations.inventory_reconciled` | Exact boolean `true` only after complete current target inventory is reconciled | Requires the full relevant Engine/container inventory and the host/supervisor identity to match the retained launch, target, and manifest. Missing or partial inventory means unknown; do not assert true. |
| `attestations.writers_excluded` | Exact boolean `true` only after effective writer exclusion is established | Requires evidence for every relevant Hub/executor/legacy writer and the accepted maintenance/stop boundary. Metadata reads alone cannot prove that a writer is stopped or cannot restart. Any stop, disable, or permission change is a separate host effect requiring explicit operator authorization. |
| `attestations.unresolved_effects_absent` | Exact boolean `true` only after both operation histories and current effects reconcile | Compare Hub command/run/reservation history with executor operation/receipt state and actual current Engine effects. Unknown command outcomes, unmatched records, unresolved work, or incomplete history remain held; never clear fences, retry commands, or manufacture a receipt to make records agree. |

The parser checks strict field sets, types, hashes, bindings, and expiry. It does not discover inventory or validate the real-world truth of the three operator attestations. The existing startup gate remains closed until a fresh operator explicitly accepts the exact challenge through the authenticated, CSRF-protected Hub route. Acceptance is not task execution authorization.

## Collection boundary

**Potentially read-only observations using existing authorized access:** inspect retained release/launch records; inspect current Engine identity and complete target inventory; read Hub and executor operation histories; inspect protected path ownership, modes, ACLs, mount identity, and backup records; and perform the existing executor connection check. Preserve the exact source, timestamp, command/API identity, and result for each observation. The connection check's two GETs establish transport/authentication only; they do not prove executor readiness, inventory completeness, writer exclusion, or permission to submit a command. Do not disclose the credentials used to make a protected read.

**Separate host effects and approvals:** stopping or disabling writers; changing service selection/entrypoint, Compose, mounts, permissions, ACLs, firewall/proxy, credentials, enrollment, owner/ledger state, backup/restore state, or executor lifecycle; accepting the manual challenge; submitting a queued job; and performing deployment/rollback/restart/reboot. This worksheet authorizes none of these. Do not execute them as a way to improve an evidence report.

If a required source cannot be read without crossing an unapproved boundary, record the evidence as unavailable and stop. Do not fill the attestation, create the session file, or use an old report as a substitute.

## Prepare and validate the colocated session file

After an authorized operator has collected and independently reviewed the
actual release, launch and host evidence in the tables above, assemble the
closed JSON object described in [Trusted manual staging startup](trusted-manual-staging-startup.md).
Copy the original observation time and launch approval expiry; do not refresh
them when writing the file. Set `report.executor` to the exact retained launch
fields plus the enrolled release `image_config_id`, and set `evidence_sha256`
to `sha256:` followed by the lowercase SHA-256 of the exact UTF-8 bytes of the
`evidence` string. Include the three true attestations only when the operator
has actually verified each one. Never insert placeholders to make a file pass.

Place the reviewed file at an explicit absolute path, owned by the coordinator
UID with mode0600 under its private mode0700 parent, on a read-only mount.
The separate coordinator configuration must meet its own protected-file rules.
In the intended coordinator process context, run the non-activating selector:

```text
python -m hub_container_executor.installed_coordinator validate --config /absolute/coordinator.json --session-config /absolute/manual-session.json
```

It exits 0 without a success message when the protected reads, core API check,
closed schema, static bindings and freshness checks pass. Exit 2 reports a
generic error; it does not identify which check failed. Review the protected
inputs and their provenance rather than weakening checks to obtain a pass.
Validation does not open secrets, contact the executor, accept the challenge,
or prove operator authorship, writer exclusion, current host state or health.
The file can expire after validation; serving repeats the relevant checks and
still requires the named human's acceptance and a fresh signed observation.

## Existing evidence and scope

The isolated Linux TEST campaign `5550ee4d-2ce0-4ed3-8aca-d7dcca8bcdbf` already has its separately documented failure/rollback acceptance. This worksheet does not ask to repeat that generic pilot. It prepares only the missing provenance/evidence review for a future exact NAS-owned manual session. Real NAS storage, executor route, current Engine inventory, writer exclusion, state reconciliation, and restart/restore acceptance remain distinct live evidence; the historical campaign does not establish them.

The per-PC session-Hub bridge and queued staging dispatch are separate features; this worksheet does not select the bridge or change its grants. This document does not authorize live activation. Each host effect must have its own applicable authority and acceptance, and this worksheet does not revoke or require repeating an existing matching authorization.

## Field-reference check

Checked against source at `9841480fed721bd56ead12c573cbd6a9fbdee0d6`:

- `src/engineering_hub/trusted_queued_session.py`: `SCHEMA`, `ManualSessionEnrollment`, `parse_enrollment`, `_report_executor`, and original freshness checks.
- `src/engineering_hub/current_executor_observation.py`: exact `CurrentExecutorReleasePins` and `CurrentLaunchPins` field names and types.
- `src/engineering_hub/startup_reconciliation.py`: `OperatorReconciliation` fields and exact `true` checks for inventory, writer exclusion, and unresolved effects.
- `docs/trusted-manual-staging-startup.md`: original age/expiry bounds, separate provenance review, fresh challenge, and explicit no-activation limitations.
- `docs/live-staging-operator-gates.md`: independent current Engine/host reconciliation, effective writer exclusion, unknown-effect hold, restart/reboot and end-to-end acceptance boundaries.

This check verifies documentation-to-source field names and described policy only. No tests, imports, builds, live inventory, host access, network, or activation were performed.
