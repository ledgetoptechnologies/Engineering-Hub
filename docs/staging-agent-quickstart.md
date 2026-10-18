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

1. Beau creates a Hub task. An agent claims it, proposes a full lowercase
   40-character candidate commit, and a **different** agent approves that exact
   commit using the existing `/api/hub/v1/tasks/...` review workflow.
2. The task's owning agent submits a registered action, environment, task UUID,
   exact reviewed source commit, and `sha256:` artifact digest. The target is
   immutable after submission. A digest identifies the requested artifact; it
   does not by itself prove provenance or execution success.
3. A worker uses its own authenticated agent identity and a fresh canonical UUID
   for **this process instance** to claim work. Keep that UUID stable only within
   the process. The server supplies a fencing generation; retain the returned
   job ID, instance UUID and fence for renewals/completion. Another agent or
   restarted process must not impersonate that lease.
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
