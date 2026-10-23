# Bounded manually reconciled staging session — source component only

`engineering_hub.startup_reconciliation.StartupReconciliation` supplies an
in-memory admission capability. Both explicit queued-service compositions and
`QueuedContainerPilot` now require this boundary; it enables no service, queue,
deployment or recovery operation by itself. Ordinary Compose, normal non-pilot
startup, NAS configuration, credentials and production remain unchanged.

Every instance starts closed with a fresh internal challenge. Trusted local
operator composition can accept one exact `OperatorReconciliation` report bound
to its actual `PilotOwner` capability and current epoch, target, manifest and
independently enrolled executor generation/artifact. Only the fixed
`engineering-hub:staging` target is supported. There is no persisted approved bit,
CLI/environment/JSON/HTTP opener, default trust callback, signing key or new service.

This Python capability is not human authentication. Report booleans explicitly
record operator attestations; neither their truth nor a report digest proves
inventory reconciliation or writer exclusion. A trusted operator channel and its
launcher integration still require independent design/review. Do not expose the
accept method through a generic MCP tool, settings file or public API.

The required synchronous identity observer must independently verify the current
executor against accepted enrollment; returning an expected object or a boolean
allowlist is not that verification. The component requires the exact typed
identity and rechecks owner/epoch, process and observer identity before opening
and on every `assert_permitted`. The finite lifetime (1–3600 seconds) begins at
construction, includes review/observer time and cannot be renewed. Expected
readiness denial while acceptance is pending does not consume the acceptance
attempt. Identity/expiry failure while pending, a binding failure after acceptance,
any failed acceptance or explicit close seals the instance closed. A new
instance needs new independent operator acceptance. Callbacks are cooperative:
elapsed-time checks cannot interrupt blocked I/O, so the caller must bound actual
observer operations. No cancellation or adoption of returned Tasks occurs.

## Trusted-operator recovery procedure still required

Before accepting a bounded session, the operator must stop/exclude previous Hub,
executor and competing target writers; preserve complete state; select the intended
authority; and reconcile current Engine containers, operation journals, reservations,
immutable artifacts, host boot/process/generation and resource/mount boundaries.
Inventory must include work absent from the restored Hub history. A fresh challenge
and old terminal command receipts alone do not establish current-host reconciliation.
Unknown effects remain held; do not replay commands, reset/bump fences, release
reservations or silently adopt a stale authority to make the report pass.

The existing independently trusted launcher may eventually own this acceptance
boundary; no third monotonic service is inherently required for a manually accepted
first release. That model trusts the operator, excludes malicious undisclosed
administrator rollback, and requires new acceptance after process restart/reboot.
It is not automatic anti-rollback detection. Restoring both histories, or changing
the Engine after inspection, cannot be made safe merely by this local component.
Actual exclusive-writer enforcement/current-host acceptance remains mandatory.
Do not restore a live database underneath an active owner.

## Mandatory source integration; operator channel still missing

Both explicit services require a trusted synchronous
`reconciliation_factory(owner, target)`, returning the exact bound
`StartupReconciliation` instance. This factory is an explicit trust dependency,
not evidence that the attestations are true. It may return a pending gate; no
implicit acceptance occurs. Invalid/async results do not transfer authority.
An unstarted returned coroutine is closed without execution. A returned local
Task/Future is cancelled and retained until its cleanup finishes before the
owner closes, including repeated service cancellation. The internal operation
and known outer service lifecycle tasks are not cancelled/gathered into themselves;
foreign-loop Tasks and custom awaitables
are not adopted or cancelled. Trusted factories must not start unreturned work
or lend owner capabilities to unowned/foreign tasks. Returned work must not depend
on service completion; arbitrary transitive task-wait cycles cannot be inferred
from an awaitable's return value. Those factory contract violations cannot be
made resource-safe by inspecting the return value.
Accepted bound gates transfer lifecycle ownership to the service and are closed
with admission shutdown and cleanup, before closing the caller-owned ledger.

Queued target submission, claims, direct bridge execution and the bridge's central
pre-effect authorization now require current gate permission. The native runtime
does not claim pending work merely because its event feed starts. A pending gate
can later be explicitly accepted by trusted composition; admission is retried on
the next ordinary feed wake, not via a new polling worker. The bridge exposes only
sanitized local `reconciliation_state`, not a remote-readiness claim. Public
runtime/autonomy state projects `startup_reconciliation_required` or
`manual_session_closed` instead of `active` while its observer is alive but its
gate is pending/closed. Status reads check only the local monotonic deadline,
sealing expiry closed without identity-observer or owner I/O; accepted
state is local acceptance, not current executor health. Actual admission checks
may invoke the synchronous observer multiple times across queue/runtime/phase
boundaries; each observer call must have its own cooperative I/O budget.
Shutdown or
native-source failure closes the gate synchronously but retains/drains already
accepted operations. Gate permission is rechecked before every next effect;
successful checks are never reusable permit tokens.

The installed coordinator and unified selector Python functions require an
explicit trusted reconciliation factory to activate. Their existing CLI/JSON
paths cannot supply it and refuse activation before credential resolution or
owner construction. Validation remains non-activating. This intentional refusal
must not be bypassed with a JSON approved bit or an allowlist-generated report.
The real trusted operator/launcher channel is the next source-design gate; this
patch does not make those CLI serve paths or live NAS staging usable.

Original-command GET inspection may remain available under its existing separate
authorization; reading receipts must not open the gate or authorize effect replay.
Historical recovery may persist original-command evidence while closed, but both
the bridge's final reconciliation step and the queue reconciliation verifier
require current gate admission before releasing a held machine reservation.
Ordinary accepted in-flight terminal completion retains its existing proof/drain
path; closure does not retroactively cancel accepted effects or invalidate their
independently verified completion.

Tests use real Linux `PilotOwner` and synthetic observers/operator reports. They
cover challenge/report replay, exact binding, invalid booleans/lifetimes, observer
failure/async results, expiration and fork/owner loss. They do not establish NAS
ACLs, current Engine inventory, a real launcher channel, machine-wide restore
safety, live execution, coherent protected restore or PC reboot acceptance.
