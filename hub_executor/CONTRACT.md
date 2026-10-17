# Engineering Hub V1 fixture executor contract

This is the Codex-facing typed, authenticated, fixture-only adapter contract. It is **not** a real deployment adapter. Only `3d-viewer:staging` is configured, and its server-side driver is `fixture`; it never resolves to a live/staging host. No request accepts a URL, path, shell text, command, or adapter selector.

## Authentication and transport

All routes except `GET /health` require `Authorization: Bearer <configured-token>`. The service compares the configured token without logging or returning it. The controller client must require HTTPS except for a literal loopback HTTP endpoint used by the isolated Docker harness. This does not specify backend-to-real-host TLS or credential provisioning. The fixture server caps connection workers at 16 with a one-second socket idle timeout; this is not a public-production DoS defense.

**Docker network deviation:** Docker Engine 29.5.2 in this environment did not allocate a published host port for a service on a Compose `internal:true` network (`NetworkSettings.Ports["8080/tcp"]` was null). To retain a real Docker-published loopback port, the smoke uses a dedicated bridge with `com.docker.network.bridge.enable_ip_masquerade=false`; the harness verifies this option and the `127.0.0.1` port binding. This is not the exact `internal:true` flag from the requested container profile and must be rechecked on the parent integration host. The fixture service makes no outbound network requests.

The no-masquerade bridge is not an egress firewall and does not prove that host/LAN services are unreachable. This harness runs only the fixed fixture service, never untrusted repository code. Do not reuse this profile for untrusted build execution. The host scratch parent is mode0700, the isolated data root2770, and the temporary secret0440; UID/GID568 receives only the fixture owner's supplementary group for that private mount. No live credentials or Docker socket are mounted.

JSON bodies are bounded, have exact documented top-level keys, reject unknown/duplicate fields, and reject invalid types. Error bodies contain only a machine-readable code:

```json
{"error":{"code":"invalid_payload"}}
```

No error includes exception detail, request contents, filesystem paths, credentials, or secret values.

## Fixture manifest

The test configuration pins:

- target: `3d-viewer:staging` (driver: `fixture`)
- `candidate_commit`: `0123456789abcdef0123456789abcdef01234567`
- `policy_revision`: `1`
- candidate artifact `sha256:4c031a5fbabf2e39b2aa313c390470863285ffea20396317c6c996a8e1fb8725` → bytes `engineering hub fixture v2\n`, bound to candidate commit `0123456789abcdef0123456789abcdef01234567`
- rollback artifact `sha256:3ab6e82eb9e517ba1b2a107b4d8b838e7e872a4f7a62c068a07b07d4227103ec` → bytes `engineering hub fixture v1\n`, bound to prior commit `fedcba9876543210fedcba9876543210fedcba98`

The request supplies a digest, commit, and policy revision, but none grants permission: the server requires exact equality to the configured immutable digest→(bytes, commit) manifest and server-owned policy revision. The rollback artifact is known before any fixture effect. These are fixture values, not production policy or release evidence.

## `POST /v1/commands`

Exact request keys:

```json
{
  "command_id":"550e8400-e29b-41d4-a716-446655440000",
  "target_id":"3d-viewer:staging",
  "action":"deploy_fixture",
  "artifact_digest":"sha256:4c031a5fbabf2e39b2aa313c390470863285ffea20396317c6c996a8e1fb8725",
  "candidate_commit":"0123456789abcdef0123456789abcdef01234567",
  "policy_revision":1,
  "fencing_generation":1
}
```

`command_id` is a canonical UUID string, including UUID5. `target_id` is an operator-allowlisted canonical `<project>:staging` identifier. `action` is exactly `deploy_fixture`, `smoke_fixture`, or `rollback_fixture`. `artifact_digest` is `sha256:` plus 64 lowercase hex characters and must exist in the immutable fixture map. `candidate_commit` is 40 lowercase hex characters and must equal the configured fixture provenance. `policy_revision` and `fencing_generation` are positive JSON integers (booleans are rejected); fencing_generation is bounded to SQLite's signed64-bit positive range, maximum9223372036854775807.

A command response has the accepted request fields at top level plus `state`, `reason`, and `observed` evidence; an exact idempotent replay may also contain `replayed:true`. States are `pending`, `running`, `succeeded`, `failed`, or `held`; `reason` is null or a machine-readable code. Example successful new command (`201 Created`):

```json
{
  "command_id":"550e8400-e29b-41d4-a716-446655440000",
  "target_id":"3d-viewer:staging",
  "action":"deploy_fixture",
  "artifact_digest":"sha256:4c031a5fbabf2e39b2aa313c390470863285ffea20396317c6c996a8e1fb8725",
  "candidate_commit":"0123456789abcdef0123456789abcdef01234567",
  "policy_revision":1,
  "fencing_generation":1,
  "state":"succeeded",
  "reason":null,
  "observed":{"active_artifact_digest":"sha256:4c031a5fbabf2e39b2aa313c390470863285ffea20396317c6c996a8e1fb8725","effect_count":1,"evidence_valid":true}
}
```

A new accepted command returns `201`; an identical UUID+payload replay returns `200` with the existing representation, optionally `replayed:true`, and never applies a second filesystem effect. A reused UUID with different payload returns `409 command_conflict`. The fencing generation belongs to the RUN, not each command: deploy/smoke/rollback may share it, with only one accepted command identity per target/generation/action. A fresh ID cannot repeat a phase. Generations below the durable high-water return `409 stale_fence`; running/held commands block new target commands with `409 target_busy`, including a proposed newer generation, until outcome reconciliation. The high-water and pause state are rechecked immediately before each bounded effect. A newly observed pause holds the accepted command with `paused_before_effect`; it is never auto-replayed on resume. Policy provenance mismatch returns `409 policy_conflict`. A paused executor rejects new admission with `423 paused`. No rejected new command executes. A timeout after acceptance is UNKNOWN: query by the stable UUID and explicitly reconcile; do not blindly replay under a new UUID.

## Local ownership and restart

This fixture executor is Linux-only. Its CLI holds a kernel `flock` service-owner lock for its entire lifetime, so a second process using the same private data root fails startup without adopting state. Core initialization/recovery and active command execution share a second nonblocking kernel lock; a second object cannot convert a genuinely running foreign command to held. Only after acquiring ownership may restart recovery mark orphaned running rows held. Identical in-flight command IDs retain read-only replay/status semantics; they never take over execution. Never delete/replace lock files while an owner is live, run mixed old/new writers, or put this SQLite/lock state on SMB/NFS. Locks are local-process ownership, not distributed leases or proof of host isolation.

## `GET /v1/commands/{id}`

Read-only durable status/evidence; returns `200` with the command representation or `404 {"error":{"code":"command_not_found"}}`. GET never applies or retries an effect. A 404 means absent on this adapter, not proof that no other actor ran work.

## `POST /v1/commands/{id}/reconcile`

Explicit inspect-only reconciliation against actual atomic fixture effect evidence. It may update a held command ledger, but MUST NOT execute/replay an effect. A matching marker, actual artifact-byte digest and valid executor-minted HMAC receipt can resolve a held command to `succeeded`; absent, malformed, forged or conflicting evidence remains `held` with a reason and no unverified target metadata. The private receipt key lives in the executor ledger, not the target and never an API response. Preserve that ledger/key coherently across restart/backup; legacy unsigned target state cannot certify success. The key/ledger must remain trusted and inaccessible to workloads: this does not resist an actor controlling the ledger, Docker daemon, or unrestricted same-OS identity. Successful reconciliation returns `200` with the command representation. No request body is accepted.

## `POST /v1/pause`

Authenticated admission control; exact request shape `{"paused":true}` (or `false` to resume). Returns `200 {"paused":true}`. Pause state is durable and blocks new dispatch; an active action reaches only its declared atomic safe boundary, with no force-kill.

## `GET /health`

Unauthenticated liveness only: `200 {"ok":true}`. It reveals no configuration, token, command data, or host state and is never interpreted as pilot activation.

## Error/status mapping

Error bodies use `{"error":{"code":"..."}}`; no secret or exception output is permitted:

| HTTP | code | Meaning |
|---|---|---|
| 400 | `invalid_payload` | malformed/oversized JSON, unknown/duplicate fields, invalid UUID/type/shape |
| 401 | `unauthorized` | missing or forged bearer token |
| 403 | `target_or_artifact_not_allowed` | unknown target or artifact digest |
| 404 | `command_not_found` | absent command on this adapter |
| 409 | `command_conflict` | UUID already bound to another exact payload |
| 409 | `stale_fence` | generation is below durable target high-water or loses immediate pre-effect recheck |
| 409 | `target_busy` | target cannot be safely admitted due to an active conflicting operation |
| 409 | `policy_conflict` | candidate commit or policy revision differs from server-owned fixture manifest |
| 423 | `paused` | executor admission is paused |

Unexpected internal failures must not expose details; if an HTTP error response is sent, it contains only a generic safe code.

## Scope and test levels

V1 uses only immutable tiny fixture files and atomic marker/state-file effects. It does not run shell commands, timers, arbitrary jobs, network fetches, real deployments, real smoke suites, or real rollback. Tests cover (1) core ledger/filesystem behavior, (2) authenticated HTTP behavior, and (3) a real isolated Docker container with read-only root, UID/GID 568, dropped capabilities, no-new-privileges, bounded resources, the documented no-masquerade bridge deviation, random loopback-only published port, and a scratch-only bind mount. Docker success is not production policy, adapter, TLS, TrueNAS/ZFS, or boot acceptance. Normal Hub startup configures no fixture workflow or host dispatcher: run creation/dispatch returns HTTP423 `executor_not_enabled`. The explicitly injected isolated test workflow enables fixture dispatch only; it never enables host dispatch or authorizes a real application rollout.

Cross-backend acceptance: put the exact Codex Hub checkout's src/ and this worktree on PYTHONPATH, then run `python docker/hub-test-integration.py --hub-commit <full-Hub-SHA>`. It verifies imported Hub provenance and actual container HTTP for success, explicit test-only state corruption/rollback, an actual response discarded after commit/unknown held with GET-only inspection, pause and real container restart. No executor response is fabricated. `tests/test_executor_hub_integration.py` also verifies whole Hub REST assignment, distinct-agent review and dispatch through a real loopback HTTP executor, with an internal fixture human session only, never real OAuth availability.
