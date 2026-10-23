# Bounded manually reconciled staging session — source component only

`engineering_hub.startup_reconciliation.StartupReconciliation` supplies an
in-memory admission capability. It is **not integrated into any startup path**
and enables no service, queue, deployment or recovery operation. Ordinary Compose,
NAS configuration, credentials and production remain unchanged.

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

## Later integration contract

Independently review the trusted operator/launcher path first. Then bind this gate
to queued target submission, claims and the bridge's central runtime admission
hook so `create_run`, `advance` and every next `command` are covered. Recheck before
each next effect; never cache a successful return as a reusable permit. Sticky
closure must stop new admission but retain/drain already accepted operations.
Original-command GET inspection may remain available under its existing separate
authorization; reading receipts must not open the gate or authorize effect replay.

Tests use real Linux `PilotOwner` and synthetic observers/operator reports. They
cover challenge/report replay, exact binding, invalid booleans/lifetimes, observer
failure/async results, expiration and fork/owner loss. They do not establish NAS
ACLs, current Engine inventory, a real launcher channel, machine-wide restore
safety, live execution, coherent protected restore or PC reboot acceptance.
