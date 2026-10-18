# Local application pilot: proposed contract

Status: design for peer review, not an enabled capability or deployment approval.
This is separate from the file-fixture executor. Existing `*_fixture` actions
retain their original meanings. The first application target is a separate Hub
instance, not the controller orchestrating its own replacement.

The synchronous pilot client has a bounded 125-second whole-operation deadline
by default (and maximum). A configured deadline must cover the manifest's
`total_command_ms` plus five seconds for transport. Workflow construction, run
creation and phase dispatch reject incompatible client/manifest pairs before
reservation or effects. Missing capability is not inferred optimistically from
an injected adapter. Unknown outcomes still remain held under the original
command identity; no timeout permits blind retry or asynchronous takeover.
Focused controller/client/API regressions pass (168 tests), including the maximum
120-second admitted host budget and rejection without creating runs/effects.
These tests use synthetic transport, not actual host execution.

## Ownership and boundaries

Codex owns the typed controller client, workflow, dispatcher integration and Hub
presentation. Hermes owns the host adapter and isolated acceptance harness after
agreement on this contract. Claude independently reviews the candidate and user
experience. Shared host access does not grant competing deployment ownership.
The canonical topic/message registry is a separate bus-owned source slice.

The adapter runs outside both Hub containers. Neither receives a Docker socket.
The first campaign uses only trusted reviewed Hub images and synthetic credentials
on disposable infrastructure, with no existing containers, NAS mounts or live
application state. A Docker command allowlist is not an OS sandbox against a
compromised adapter with general daemon access. No untrusted builds are admitted.

## Proposed wire shape

Use a separate `/v1/container-pilot/commands` namespace and capability
`container_pilot_v1`. Requests have exactly these fields:

| Field | Constraint |
| --- | --- |
| schema_version | literal `container_pilot_v1` |
| command_id | canonical UUID; stable per run and action |
| run_id | canonical UUID bound to one reviewed Hub run |
| target_id | literal `engineering-hub:staging` for this first pilot |
| action | one of the four actions below |
| manifest_digest | SHA-256 of the complete canonical operator manifest |
| candidate_commit | exact lowercase 40-character source commit |
| policy_revision | positive integer, not boolean |
| fencing_generation | positive signed-64-bit integer, not boolean |

Actions are `deploy_container`, `smoke_container`, `rollback_container` and
`smoke_container_rollback`. Rollback health requires its own phase: successful
container creation/start is not proof of successful rollback. Per-run phase
dedupe must not be bypassed by a fresh command UUID.

GET by command UUID is read-only. Explicit bodyless reconcile inspects the
original effect without replay. Unknown outcomes stay held until inspected;
no timeout, lease expiry or new process grants permission to repeat a Docker
effect. Responses echo every request field and report state, fixed safe reason
and bounded actual-container evidence. No exception, secret, host path, session
ID or arbitrary Docker output enters public records. The controller verifies
the entire echo, not merely the returned command ID or HTTP status.

## Operator manifest, not caller authority

Before adapter implementation, agree the exact canonical manifest schema shared by both
ends. It must bind target/campaign, driver version, policy revision, candidate
and rollback **Docker image config IDs**, corresponding source commits and build
provenance, complete runtime profile, state/rollback policy and health probe.
Registry manifest digests are a different type; do not interchange them with
local Docker image config IDs despite their similar `sha256:` syntax.

Images must already exist locally and be immutable selections (`--pull never`).
All runtime authority is operator-owned: container names/labels, private volume
identities, UID/GID 10001, resource limits, network policy, environment, probe and
bounded timeouts. A request supplies no image tag, URL, path, shell, Docker option,
context, environment or mount. Pin the manifest digest in the Hub run before I/O;
changing a manifest requires a new reviewed run, not editing a pending request.

Preserve the previous application state unchanged. Candidate state is a separate
generation initialized from a verified coherent backup, not the previous live
volume. The first pilot permits no external writes or schema migrations. Rolling
back an image does not reverse a database migration. Verify previous image,
state identity and real HTTP health before recording `rolled_back`.

## Durable effects and verification

Keep controller and executor ledgers outside the deployed app. Retain stable
command IDs, target reservation, per-run fence, independent review, bounded
repair budget and emergency pause. Use kernel single-owner exclusion on private
local storage, never SMB SQLite/lock files. Reserve before I/O; inspect exact
daemon container ID, image/config, mounts and ownership metadata after an
uncertain result. Labels or a file receipt alone cannot certify a real container.
Do not silently start an exited workload during reconciliation.

Acceptance must exercise actual app rollout and HTTP/assets/auth-disabled smoke,
a declared real candidate startup/health failure and actual data-safe rollback,
then controller/executor restart with retained ledgers. Kill before Docker effect
and after the real effect but before acknowledgement. Exact replay, stale fences,
conflicting IDs, concurrent ownership, pause, foreign-container name collision,
unexpected image/config/mount and unavailable daemon must remain safe. Inspect
actual daemon effects to prove no duplicate creation/start after an uncertain
outcome. Cleanup may remove only the campaign's exact verified resources; never
use a global prune or delete an unfamiliar volume/container.

None of this substitutes for dedicated OAuth setup, canonical topic attachment,
TrueNAS storage/backup checks, staging-host writer exclusion or actual host reboot
acceptance. The default Hub startup remains non-dispatching until explicitly
configured with a reviewed workflow and operator manifest.

## Controller source slice (not actual app acceptance)

`container_pilot_client.py` and `container_pilot.py` implement the separate typed
client/controller. Explicit constructor injection enables only the new
`/api/hub/v1/container-pilot/runs` and run dispatch namespace. Existing fixture
routes remain disabled when only the pilot workflow is injected. Both workflows
cannot be configured together in this first slice. No environment setting, agent
request or default entrypoint constructs a pilot workflow or selects a daemon.
The overview reports `container_pilot_dispatch` independently of fixture support.

The controller binds the complete operator manifest digest in each reviewed run
and durably pins its narrower immutable view before creating runs. That view
includes candidate/rollback image config IDs and source commits. The shared
schema now validates and hashes the full manifest as well; the host adapter must
still independently verify its provenance, resolved policy and actual effects.

Wire responses echo every request field and add `state`, nullable fixed `reason`,
`evidence` and `replayed`. Evidence fields are exactly `container_id` (64 lowercase
hex or null), `image_config_id` (SHA-256 or null), `health`
(`unknown|healthy|unhealthy`) and strict boolean `evidence_verified`. These remain
adapter assertions, not independent Docker verification. Both health phases
require a matching image/container plus healthy evidence; start/create success
alone cannot complete the run or rollback. Unknown outcomes preserve stable IDs,
hold reservations and never trigger automatic repost or takeover.
The private command ledger retains the exact bounded typed adapter response,
including mismatched evidence that caused a hold. Unknown outcomes retain null
evidence rather than an invented success. Older unknown reservations gain only
the nullable evidence column on upgrade; absence never permits resubmission.

The draft reason vocabulary is `inspect_required`, `paused`, `target_busy`,
`stale_fence`, `identity_conflict`, `manifest_mismatch`, `policy_mismatch`,
`image_unavailable`, `daemon_unavailable`, `container_conflict`, `evidence_mismatch`,
`health_failed`, `effect_failed` and `recovery_required`. Adapter agreement and
actual image/state/health/restart acceptance remain outstanding. MockTransport
and typed-fake controller tests are not an application pilot.

## Shared manifest source profile

The shared `container_pilot_manifest.py` schema validates every nominated section,
rejects unknown/duplicate JSON keys, floats/non-finite numbers and numeric values
masquerading as security booleans. The fixed profile is a first Hub campaign,
not a generic container service: Linux amd64/arm64, UID/GID10001, private PID/IPC,
read-only root, all capabilities dropped, no-new-privileges, no privileged mode,
no network or published ports, and a private state generation at `/var/lib/hub`.
Health is checked inside the exact selected container, never at an agent URL.
Candidate and rollback each require the complete compiled health/app-assets/auth-
denial probe sequence. Memory/CPU/PID/scratch/timeouts and retention are bounded.

All three agent secrets refer to distinct opaque **test-only versioned slots**,
never values, hashes of weak secrets or a mutable latest alias. No resolver or
credential is provisioned by this schema. Image defaults are selected through
the immutable image configuration and `image_default` entrypoint/command policy;
the future driver must validate the effective configuration against both that
image and the manifest, not silently accept daemon defaults or extra environment.

Canonical UTF-8 JSON uses sorted keys, compact separators, explicit required
fields/nulls, integer-only numeric fields, no Unicode normalization and no self-
digest/signature fields. Both ends must recompute SHA-256 from the complete
validated object. The controller's `ContainerPilotTarget.from_manifest` factory
derives its narrower view from those bytes. Digest-only legacy targets are not
enough to configure a workflow. Canonicalization revalidates model data, so
`model_copy`/nested changes cannot bypass policy by retaining a frozen wrapper.
These are syntax/policy checks, not proof of provenance, coherent state backup,
resolved secrets, isolated daemon identity or actual Docker behavior.

### Docker identity caution verified on the development host

On this host's Docker29.8/API1.56 containerd store, an unqualified image inspect
returned an OCI **index** in `.Id`; a platform-qualified inspect returned the OCI
**manifest**, not the configuration digest. The observed descriptor media types
made the distinction explicit. The official [Moby implementation](https://github.com/moby/moby/blob/master/daemon/containerd/image_inspect.go)
sets that field from the selected target descriptor. Never promote a generic
Docker `.Id`, `RepoDigest` or container `.Image` to `image_config_id` by renaming
it. The adapter must verify the platform/index/manifest/config relationship and
the actual selected container; syntactically identical SHA-256 strings are not
interchangeable identities. Read-only metadata inspection does not establish a
deployment, review or build-provenance acceptance.

The bounded read-only helper `scripts/inspect_hub_image_identity.py` streams a
local Docker image archive without extracting it or writing a host archive. It
verifies the selected index/platform-manifest/config metadata SHA chain and
prints only the selected identities. It does not authenticate skipped layers,
resolve secret slots, attest a build or prove a deployed container matches.
It accepts only an explicit immutable SHA-256 selection and Linux amd64/arm64
architecture choice; actual ARM variant/host admission still belongs to the
adapter. Use no platform rewrite when exporting an index: on this Docker version
that rewrite removed the original index from the archive, preventing proof of
the requested index's relationship to the platform config.

Root independently exercised this helper against an existing synthetic image:
index `d692f0304cbfa318a86f16925b00ab87b2b92ab8b3c19b77edbe1fcd69f831d6`
selected platform manifest `5e4fc6400b4d21cc28dff2459c01906fcb886ce3b1e00dbe17bb5c1bb8aa6eb7`
and config `b3d1703a7c6f7d9aaaa5ba8907c6b42d583bcb659aabad3072ad317d6cf1caf9`.
The read-only operation exited zero; it neither launched nor changed a container.
