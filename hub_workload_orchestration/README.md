# Viewer/NodeODX workload orchestration proposal

This directory is a **source-only, inert interface proposal** for review. It is not
an executable processing bridge, scheduler, provider adapter, admission policy,
or live integration. It is not registered in the current default application and
no packaging configuration was changed to install or activate it.

## Proposed intent API

```python
from hub_workload_orchestration import ViewerProcessingIntent

intent = ViewerProcessingIntent(
    code_target=existing_work_target,  # an existing engineering_hub WorkTarget
    submission_id=canonical_submission_uuid,
    viewer_project_id=viewer_project_key,
    dataset_id=dataset_key,
    provider_id=provider_key,
    input_manifest_digest="sha256:" + "a" * 64,  # placeholder shape, not provenance
    options_digest="sha256:" + "b" * 64,  # placeholder shape, not provenance
)
```

`ViewerProcessingIntent` is frozen and accepts only its declared constructor
fields. `project` is fixed to the existing Hub project `3d-viewer`; its default is
that value and any other value is rejected. `code_target` must be the exact
existing `engineering_hub.work_queue.WorkTarget` type. Its
`artifact_digest` is retained unchanged and is distinct from both data digests.
No WorkTarget, reviewer, approval, machine, action, or worker is created by this
proposal.

`submission_id` must be the canonical lowercase UUID string. The opaque project,
dataset, and provider IDs must be nonempty ASCII keys matching the current
WorkQueue key rule: `[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}`. Values are validated but
never normalized. Both data digests must match `sha256:[a-f0-9]{64}`. This envelope
contains references/digests only, not dataset bytes, paths, URLs, tokens, or
commands.

## Read-only provider observation

```python
from hub_workload_orchestration import observe_provider_info

observation = observe_provider_info(
    {"taskQueueCount": 2},  # synthetic example, not a provider response
    provider_kind="nodeodx",  # or "clusterodm"
)
```

The helper accepts an exact built-in `dict` and one closed provider kind:
`nodeodx` or `clusterodm`. It reads only `taskQueueCount`; all other input keys
are ignored. `ProviderQueueObservation` is a frozen, allowlisted DTO containing
only `kind`, `queue_count`, `state`, and fixed `admission_allowed=False`.

- A valid positive integer count reports `queued_work_observed`.
- A direct NodeODX count of zero reports `no_queued_work_observed` (not idle).
- ClusterODM zero retains `queue_count=0` but reports `unknown`, since the
  provider-managed counter is not evidence of physical capacity. Missing values,
  booleans, negative numbers and other invalid counts report `queue_count=None`
  and `unknown`.
- Queue observation never admits work. The DTO cannot be constructed with an
  admission flag supplied by the caller, and it has no ready/idle/authorized
  state.

There is no queue payload conversion, claim, launch, provider callback, network,
filesystem, or mutation operation here. The helper does not verify that input
came from a real provider.

## Scope and open gates

This is a proposed review contract, **not a claim that these fields match the
Viewer HTTP wire schema**. Viewer HTTP adaptation, NodeODX/ClusterODM integration,
trusted data provenance, approval and production-priority enforcement remain
unimplemented. Tests use synthetic values and the real local `WorkTarget` type;
they do not prove provider behavior, provenance, approval, or deployment.

Production priority remains owned by the existing Codex `WorkQueue` policy.
The original `f68eec8` baseline allowed an action filter to skip queued approved
production and claim staging on the same machine. Codex corrected this in
`7579b06c9e5d752aaa10c0ad54b4a8bba2043d51`; this proposal now builds on reviewed
development head `2b6bfcacb8808df1b5d0c64a3fac02219b316a66`. The same synthetic
characterization now refuses that filtered staging claim. This component adds
no second priority queue or veto algorithm. Core source correction is not proof
of deployed policy or exclusion of external processing writers.

Run the focused source tests from the repository root with:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:src python3 -m unittest discover -s tests \
  -p 'test_workload_orchestration_intents.py' -v
```
