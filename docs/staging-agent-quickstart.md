# Engineering Hub staging coordination: agent quickstart

These are the **new candidate image's** HTTP endpoints. They are not available
on TrueNAS until Beau installs that image and explicitly enables the queue.
This document does not report a deployment, active worker, or successful staging
execution. Keep the existing message bus and clients unchanged during rollout.

## Connections and operator configuration

| Purpose | Address | Authentication |
| --- | --- | --- |
| Agent coordination API | `http://192.168.50.80:8770/api/hub/v1/work` | Existing distinct Codex, Claude, or Hermes bearer token |
| Human dashboard | `https://hub.ledgetoptechnologies.com` | Separate Hub Cloudflare Access application plus local Hub session/CSRF |
| Browser origin on TrueNAS | `http://127.0.0.1:8771` | Cloudflared host-network origin; verified Access assertion still required |
| Existing production message bus | Existing port `8765` | Existing message-bus token; **not** a work-queue endpoint |

The split-ingress deployment keeps the browser origin loopback-only while the
agent listener remains LAN-accessible. The actual installed listener/tunnel
topology must be verified; this source change alone does not secure the network.
LAN HTTP does not encrypt bearer tokens: use only the approved trusted LAN, or
arrange an authenticated encrypted transport before crossing an untrusted network.
Do not put tokens in commands, Git, chat, URLs, logs, or example files.

The operator enables coordination explicitly in the new Hub instance:

```text
HUB_WORK_QUEUE_ENABLED=1
HUB_STAGING_MACHINE_ID=staging-pc
```

The machine ID must identify the same physical staging computer consistently
across projects, not a different container or agent. The flag does not configure
SSH, mount data, launch a worker, or authorize deployment. Without a configured
queue the authenticated routes return `423 work_queue_not_configured`.
Enabling the queue is incompatible with the older independently dispatched
fixture/pilot workflows; do not create competing machine owners.

The flag alone permits coordination, not executable admission. The configured
queue installs a closed claim policy: `/claim` returns `{"job": null}` and status
shows `claim_admission_state: unconfigured` / `admission_not_configured`, without
reserving new work. Trusted server composition must enroll the worker, action,
environment, physical machine and exact task/commit/artifact before claims can
succeed, together with appropriate execution-evidence and recovery providers.
There is no public agent endpoint to install that policy. A process UUID is a
cooperative instance identifier, not an additional executor credential.

## Workflow and boundaries

### First-use handoff on the existing staging PC

Current checkpoint: source `6e364635b4be5c5afd3499ee0eccff8ac3f91a46`, documented
in `staging-validation-acceptance-2026-10-18.md`. No extra PC or mandatory VM is
required, but an existing host is not automatically an approved executor boundary.
Beau approved read-only staging readiness inventory on 2026-10-19 in addition
to source work; Hermes request `de1534f4-623f-426f-8955-9f02fb32e322` carries
that scope. Hermes answered it in `ad625a54-f4e5-44f7-9340-310edb8ac167`:
the existing shared rootful Docker engine runs Viewer and PA, with image-watch
and cleanup writers whose effective pilot interaction is not yet verified.
The acceptance checkpoint records the runtime findings. Hardware inventory is
not pending; writer exclusion, bounded resources and a disposable pilot boundary
remain unproven. Do not provision, change host access, enroll state or activate
an executor on the strength of this checklist or inventory approval.

The shipped `engineering_hub.__main__`/control Docker entry point still constructs
ordinary `create_app()`. It does **not** select
`queued_pilot_service.serve_owned_queued_pilot`. Queue configuration alone cannot
bridge this gap. `docker/hub-container-pilot-acceptance.py` is intentionally
disabled, not an acceptance command to run repeatedly until it succeeds.

Before implementing an installable private composition, collect these inputs
through separately authorized work, without putting credentials in this document:

| Required input | Existing source contract | Evidence needed |
| --- | --- | --- |
| Physical host and constrained executor scope | `DockerEngineBinding` in `hub_container_executor/daemon_backend.py` | Pinned runner/daemon, Linux platform, explicit socket/API and generation-root locations; exclusion from production data and other host writers |
| Immutable candidate and rollback images | `PilotManifest` in `src/engineering_hub/container_pilot_manifest.py`; `hub_container_executor/build_enrollment.py` | Immutable runnable OCI index/platform-manifest digest chain, corresponding config IDs, source commits, build provenance and independently approved build records; a config ID is not a runnable image reference, and a moving tag or matching health string is insufficient |
| Trusted running driver | `TrustedDriverIdentityVerifier` in `hub_container_executor/backend.py` | Independently enrolled implementation/launcher identity and verifier, not test assertions or a self-issued claim |
| Healthy rollback baseline and separate candidate state | `baseline_observer.py`, `baseline_receipt.py`, `baseline_snapshot.py`, `state_provider.py` | Authoritative healthy baseline, coherent backup and protected receipt; observer drafts and file copies alone are not approval |
| Private transport and test-only secrets | `ContainerPilotClient`, `hub_container_executor/asgi.py`, manifest secret slots | Approved controller-to-executor path and secure secret resolver; do not reuse live MCP data or expose the Docker socket to agents/browser |
| Ownership, shutdown and restore handling | `PilotOwner`, `serve_owned_queued_pilot`, endpoint generation protocol | Persistent local storage, single writer, verified supervisor drain budget and explicit handling of database restore/current physical-host state; expired leases do not release uncertain work |

For a separately approved read-only inventory, reuse
`scripts/hub_engine_runner_probe.py`; it already accepts explicit pinned bindings
and makes a bounded Engine info observation. It does not discover credentials,
provision its generation root, pull an image or launch containers. An `ok` result
proves only the stated runner observation, **not** driver identity, image/profile,
deployment or rollback. Do not invent pins from a synthetic fixture to satisfy it.

After the inputs and host boundary are independently reviewed, prepare the
explicit installation/entry-point wiring and obtain separate execution approval.
The first real pilot must target a separate disposable Hub instance, not replace
its controller, Viewer/PA, the live MCP or production. Record actual immutable
candidate deployment, health evidence, a deliberate failed candidate and verified
rollback, durable held handling and installed restart behavior. Keep container
fixture results separate from host/reboot acceptance. Stop at any missing input;
do not substitute a permissive callback or edit held SQL to make the pilot pass.

1. Beau creates a Hub task. An agent claims it, proposes a full lowercase
   40-character candidate commit, and a **different** agent approves that exact
   commit using the existing `/api/hub/v1/tasks/...` review workflow.
2. The task's owning agent submits a registered action, environment, task UUID,
   exact reviewed source commit, and `sha256:` artifact digest. The target is
   immutable after submission. A digest identifies the requested artifact; it
   does not by itself prove provenance or execution success.
3. Agent-facing claim calls derive the worker identity from the authenticated
   agent bearer and a fresh canonical UUID for **this process instance**. That
   does not authorize an executable action or let an agent select a private
   executor role. Actual execution needs explicitly enrolled trusted composition;
   the private local validation/pilot bridges are not remote bearer clients.
   The server supplies a fencing generation; retain the returned job ID,
   instance UUID and fence for renewals/completion. Another agent or restarted
   process must not impersonate that lease.
4. Renew before lease expiry. Expiry or an uncertain external outcome holds the
   physical machine for authoritative reconciliation; it does not prove work
   stopped and does not permit replay. Do not retry external effects merely
   because a response was lost.
5. Executable actions default to requiring independently verified completion
   evidence. `evidence_id` is only a reference consumed by an explicitly enrolled
   verifier, not proof because a worker supplied it. Missing/invalid evidence
   returns `409 work_completion_proof_required` and preserves the reservation.
   `held` preserves the machine too. Only explicitly registered coordination-only
   actions can accept a report without execution evidence. There is no HTTP
   endpoint that enrolls a verifier or generates trusted proof.

Production jobs are prioritized ahead of **eligible** staging jobs, without
preemption. Priority is not production authority. The default configuration has
no production approval verifier; production jobs can be visible but remain
blocked. Answered ordinary Hub decisions explicitly do not authorize execution.
Do not use a decision ID, arbitrary approval string, or queue completion as
permission to deploy, merge or promote.

Production precedence is checked across actions and workers on the same physical
machine, before the caller's action filter. A staging-only worker cannot bypass
an approved, currently verified production candidate merely because it cannot
run that candidate. Invalid, stale, removed or unapproved production is excluded.
Other machines remain independent; already-running work is not preempted.

Trusted local composition may inject `production_readiness_verifier(actor,
action_key, environment, machine_id, WorkTarget)`; no public payload, environment
flag or agent token installs it. It must assess all actually enrolled execution
paths, not one worker's denial or Prometheus/transport presence:

For an otherwise-valid queued production candidate:

| Readiness result | New production claim | New same-machine staging |
| --- | --- | --- |
| Literal `True` | Still requires existing approval, target and worker admission | Waits behind production |
| Literal `False` (authoritatively unavailable) | Denied | May proceed if otherwise admitted |
| `None`, error or non-Boolean | Denied when the callback is configured | Conservatively waits |
| No callback | Existing production worker-admission rules remain | Conservatively waits behind valid production |

Unknown readiness can deliberately delay staging; there is no fabricated global
readiness provider here. The callback governs new admission only, not renewal or
preemption of accepted work. Callbacks are point-in-time observations inside the
SQLite job transaction, not an atomic snapshot of external systems. Status/UI
eligibility remains advisory and does not evaluate this readiness callback or
promise a claim. Keep one canonical action/machine registry; use `action_keys`
to narrow a worker, not a worker-specific registry that hides other production.

The initial action keys are `<project>.validate`, including
`engineering-hub.validate` and `3d-viewer.validate`. Inspect `GET /work/status`
for the actual registry, proof policy, effective pause state and blocking reason.
Registration is not an executor: the initial configuration does not add commands,
production credentials or a completion-evidence provider. A worker must have its
own approved, reviewed implementation before performing external effects.

## API reference

All paths below start with `/api/hub/v1/work`. Read routes accept an authenticated
agent or human. Mutation routes require an agent, except pause which requires a
human session, allowed Origin and valid `X-Hub-CSRF`.

| Method/path | Purpose |
| --- | --- |
| `GET /status?limit=50` | Registry, pause state, counts, jobs and blocked reasons |
| `GET /jobs?limit=50` | Bounded recent job list (`items`) |
| `GET /jobs/{id}` | Fetch one exact durable job without claiming or renewing it |
| `POST /jobs` | Idempotent admission, keyed by authenticated agent + `request_id` |
| `POST /claim` | Claim one eligible job (`job: null` means no admission) |
| `POST /jobs/{id}/renew` | Renew matching live instance + fence; maximum lease 600 seconds |
| `POST /jobs/{id}/complete` | Record outcome with required evidence; no automatic promotion |
| `POST /pause` | Human-only queue pause; does not interrupt already running work |

Unknown input fields are rejected. Callers cannot supply an actor, machine ID,
worker name, shell command, host path, credential, or execution-provider URL.
Queue status/history is shared across agents; use task/job identifiers rather
than private local notes for handoffs. No automatic replay, scheduler loop, or
processing worker starts merely by calling these routes.

Claim accepts optional `action_keys`, a nonempty list of at most 32 unique
registered action keys. It only narrows the server's enrollment policy; naming
an action does not authorize a caller or a different commit/artifact. Do not
send this field on renewal. Instance ownership, fencing, production approval and
fresh independent review still apply before any effect.

The explicit source-only `QueuedContainerPilot` integration is documented in
`remote-software-staging-bridge.md`. It binds one private staging job to one
original remote pilot run; it supplies no installed event worker, listener,
credential or approved host. Public pilot routes and legacy dispatch are denied
after its durable enrollment. The ordinary application factory still rejects
mixing queue and pilot dispatch; do not remove that guard or install the bridge
merely to try the API example below. Outbound-worker versus private host-endpoint
deployment remains under review, with no host provisioning approved.

### Agent MCP interface

The candidate Hub MCP URL is `http://192.168.50.80:8770/mcp/`, not the old bus
port 8765. Existing agent bearer identities are reused. When the queue is
explicitly configured, it additionally advertises `hub_work_status`,
`hub_work_jobs`, `hub_work_get`, `hub_work_submit`, `hub_work_claim`,
`hub_work_renew` and `hub_work_complete`. Each takes a closed `payload` object
using the equivalent HTTP fields; get/renew/complete additionally carry `job_id`.
Status/list take `{"limit": 50}` or `{}`. Unknown fields and caller-provided
identity are rejected. There is no agent MCP pause, reconciliation, approval or
receipt-enrollment tool. Queue-disabled deployments retain the original nine
Hub tools, without advertising an unconfigured queue.

The Hub's authenticated `/api/hub/v1/events` SSE feed receives transactional
queue invalidations. An invalidation is not a job claim, execution permission,
or automatic agent wake; fetch the referenced job and inspect durable state.
No polling or replacement listener is activated by this source. The old bus's
push availability is independent of this Hub-native feed.

## Minimal Python connection, admission, claim and renewal

Run only after the candidate image is installed and the task is independently
reviewed. Configure `HUB_AGENT_TOKEN` through the agent's secure environment;
do not type its value into this script or a shell history. Supply real reviewed
`HUB_TASK_ID`, `HUB_SOURCE_COMMIT`, and `HUB_ARTIFACT_DIGEST` values. The code
never prints the credential or headers. Avoid HTTP debug logging.

```python
import json
import os
import uuid
from urllib.request import Request, urlopen

base = "http://192.168.50.80:8770/api/hub/v1/work"
token = os.environ["HUB_AGENT_TOKEN"]
instance_id = str(uuid.uuid4())  # Once per worker process, not once per request.

def api(path, payload=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(base + path, data=body, method="GET" if body is None else "POST",
                      headers={"Authorization": "Bearer " + token,
                               "Content-Type": "application/json"})
    with urlopen(request, timeout=15) as response:
        return json.load(response)

status = api("/status")
job = api("/jobs", {
    "request_id": str(uuid.uuid4()),  # Persist this key for admission retry only.
    "action_key": "3d-viewer.validate",
    "environment": "staging",
    "target": {"task_id": os.environ["HUB_TASK_ID"],
               "source_commit": os.environ["HUB_SOURCE_COMMIT"],
               "artifact_digest": os.environ["HUB_ARTIFACT_DIGEST"]},
})
claimed = api("/claim", {"instance_id": instance_id, "lease_seconds": 60,
                         "action_keys": ["3d-viewer.validate"]})["job"]
if claimed is not None:
    # A shared queue may return another eligible reviewed job. Validate the
    # returned action/target against your approved worker before any effect.
    renewed = api("/jobs/" + claimed["id"] + "/renew", {
        "instance_id": instance_id, "fence": claimed["fence"], "lease_seconds": 60,
    })
    # No execution or successful completion is implied by this example.
    # When independently verified evidence exists, the complete body is:
    # {"instance_id": instance_id, "fence": claimed["fence"],
    #  "outcome": "succeeded", "evidence_id": "enrolled-receipt-reference"}
```

This sample is one request sequence, not an unattended worker. It can reserve a
job without executing it: use an isolated acceptance instance, or an approved
worker prepared to manage the lease and report uncertainty honestly. Treat a
lost claim response as uncertain; inspect durable state rather than issuing a
second external effect or fabricating completion evidence.
