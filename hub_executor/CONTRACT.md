# Engineering Hub V1 fixture executor contract

This is the Codex-facing typed, authenticated, fixture-only adapter contract. It is **not** a real deployment adapter. Only `3d-viewer:staging` is configured, and its server-side driver is `fixture`; it never resolves to a live/staging host. No request accepts a URL, path, shell text, command, or adapter selector.

## Authentication and transport

All routes except `GET /health` require `Authorization: Bearer <configured-token>`. The service compares the configured token without logging or returning it. The controller client must require HTTPS except for a literal loopback HTTP endpoint used by the isolated Docker harness. This does not specify backend-to-real-host TLS or credential provisioning.

**Docker network deviation:** Docker Engine 29.5.2 in this environment did not allocate a published host port for a service on a Compose `internal:true` network (`NetworkSettings.Ports["8080/tcp"]` was null). To retain a real Docker-published loopback port, the smoke uses a dedicated bridge with `com.docker.network.bridge.enable_ip_masquerade=false`; the harness verifies this option and the `127.0.0.1` port binding. This is not the exact `internal:true` flag from the requested container profile and must be rechecked on the parent integration host. The fixture service makes no outbound network requests.

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

`command_id` is a UUID string. `target_id` is an operator-allowlisted canonical `<project>:staging` identifier. `action` is exactly `deploy_fixture`, `smoke_fixture`, or `rollback_fixture`. `artifact_digest` is `sha256:` plus 64 lowercase hex characters and must exist in the immutable fixture map. `candidate_commit` is 40 lowercase hex characters and must equal the configured fixture provenance. `policy_revision` and `fencing_generation` are positive JSON integers (booleans are rejected).

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
  "observed":{"active_artifact_digest":"sha256:3ab6e82eb9e517ba1b2a107b4d8b838e7e872a4f7a62c068a07b07d4227103ec","effect_count":1}
}
```

A new accepted command returns `201`; an identical UUID+payload replay returns `200` with the existing representation, optionally `replayed:true`, and never applies a second filesystem effect. A reused UUID with different payload returns `409 command_conflict`. A stale or equal per-target fencing generation returns `409 stale_fence`; the durable high-water is checked again immediately before each effect. Policy provenance mismatch returns `409 policy_conflict`. A paused executor rejects new admission with `423 paused`. No rejected new command executes. A timeout after acceptance is UNKNOWN: query by the stable UUID and explicitly reconcile; do not blindly replay under a new UUID.

## `GET /v1/commands/{id}`

Read-only durable status/evidence; returns `200` with the command representation or `404 {"error":{"code":"command_not_found"}}`. GET never applies or retries an effect. A 404 means absent on this adapter, not proof that no other actor ran work.

## `POST /v1/commands/{id}/reconcile`

Explicit inspect-only reconciliation against actual atomic fixture effect evidence. It may update a held command ledger, but MUST NOT execute/replay an effect. A matching marker can resolve a held command to `succeeded`; absent or conflicting evidence remains `held` with a reason. Successful reconciliation returns `200` with the command representation. No request body is accepted.

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
| 409 | `stale_fence` | generation is not greater than durable target high-water or loses immediate pre-effect recheck |
| 409 | `target_busy` | target cannot be safely admitted due to an active conflicting operation |
| 409 | `policy_conflict` | candidate commit or policy revision differs from server-owned fixture manifest |
| 423 | `paused` | executor admission is paused |

Unexpected internal failures must not expose details; if an HTTP error response is sent, it contains only a generic safe code.

## Scope and test levels

V1 uses only immutable tiny fixture files and atomic marker/state-file effects. It does not run shell commands, timers, arbitrary jobs, network fetches, real deployments, real smoke suites, or real rollback. Tests cover (1) core ledger/filesystem behavior, (2) authenticated HTTP behavior, and (3) a real isolated Docker container with read-only root, UID/GID 568, dropped capabilities, no-new-privileges, bounded resources, the documented no-masquerade bridge deviation, random loopback-only published port, and a scratch-only bind mount. Docker success is not production policy, adapter, TLS, TrueNAS/ZFS, or boot acceptance. Actual backend dispatch stays gated at HTTP 423 pending proof.
