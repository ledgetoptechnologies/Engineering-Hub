# Owned, inactive Engineering Hub

This explicit entrypoint keeps the ordinary Hub usable after a bounded staging
pilot, on the same already owner-enrolled ledger, without keeping a TEST executor
or its credentials running. It does not change the default Hub entrypoint.

## Scope and prerequisites

The operator must select the existing non-secret deployment configuration and
the same authoritative Hub database. The configuration loader still determines
the database from its state root; this change adds no alternate storage layout.
In particular, it does **not** make a group-writable SMB directory private, move
the ledger, configure a new mount, or make a live NAS installation eligible.

The ledger must already be enrolled, on a trusted local filesystem, with its
existing lock and enrollment identities intact. Its directory must be owned by
the process UID with mode0700; ledger and lock must be single-link mode0600
files. Startup additionally walks all directory ancestors without following
symlinks. Current file checks do not prove host-side bind-source rename protection,
NFSv4 ACL privacy, exclusion of legacy writers, or reboot/restore admission.

No migration, enrollment, permission repair, credential creation, reconciliation,
marker removal or old-backup fallback is performed. Missing or invalid enrolled
state fails closed. Opening the owner advances its normal process epoch; it is
not a zero-write open or evidence that an earlier remote processing operation has
stopped. The ordinary owned repository and authentication constructors also run
their existing idempotent schema initialization through guarded transactions.

## Explicit selection

Only after an operator has established those prerequisites, the source entrypoint
is:

```sh
python -m engineering_hub.owned_inactive_hub --config /operator/selected/settings.json
```

The example path is not an instruction to move settings. Use the established
configuration location; its parent currently selects the database and bus paths.
There is no default configuration, executor-profile argument or automatic mode
fallback. Work-queue enablement and bus startup-retention cleanup must both be
disabled; enabled or invalid values are rejected before binding or opening state.
Only combined ingress with fully configured Cloudflare Access is accepted.

Both ordinary front doors remain agent8770/browser8771, with the existing
Cloudflare authentication and unified bus policy. This module configures neither
tunnels nor host networking and enables no boot autostart.

## What remains usable

The owned repository has no execution principal. Ordinary tasks, decisions,
session authentication, overview and historical runs remain available. Notes use
the same existing bus knowledge store, not a second notes database. The bus's
messages and notes keep their existing storage/access boundaries; no new privacy
claim is made for them.

There is no work queue, executor client, executor profile or credential read,
workflow, processing worker, job claim, remote retry or runtime construction.
Queue and execution routes remain disabled. Existing queued, held and terminal
jobs, run history, reservations and queued-pilot admission markers are retained
without adoption, repair, release or resumption. Queue-list routes are disabled
too; this entrypoint does not pretend to provide an active queue monitor.

## Lifecycle

Listener sockets are bound before acquiring the existing ledger owner. The one
owner is held through both front doors, the core feed and the unified bus.
Shutdown closes new admission and disconnects idle read-only streams, then drains
accepted requests without a graceful-timeout force cancellation. It reuses the
existing `_serve_listeners` helper instead of the ordinary `serve_frontends`
30-second listener deadline. Repeated caller cancellation requests stop and waits
for the entire child lifecycle before closing ownership. It does not cancel an
accepted mutation or spawn a replacement runtime.

The external supervisor must still permit actual drain to finish. This source
change does not alter Docker's stop grace, prove force-kill recovery, or authorize
live deployment or restarts.

This is source/disposable-test acceptance only: it is not acceptance of a new
container image, NAS deployment, private host mapping, reboot recovery or snapshot
restoration. Selecting or provisioning a protected layout remains a separate
operator decision and implementation.

## Acceptance evidence

`tests/test_hub_owned_inactive.py` uses disposable Linux synthetic data and the
real kernel owner, local HTTP front doors and native SQLite event feeds. A
simulated prior pilot supplies completed and pending history; it is not real
host processing evidence. Tests verify inactive continuation and reopening,
ordinary tasks/auth/notes, retained aged acknowledged bus messages, absent
executor/runtime/queue construction, disabled execution, missing/malformed state
rejection, contention, bind-before-owner, construction failure cleanup, default
factory rejection, and ownership held through delayed HTTP mutations plus
repeated cancellation during bus/feed shutdown.
