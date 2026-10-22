# Private local CI execution provider — not live staging activation

`engineering_hub.development_ci_local.LocalDevelopmentCIProvider` connects the
owned queue adapter to the existing trusted one-shot CI controller **on the same
Linux host**. This is not a second scheduler, remote transport, webhook, daemon,
new CLI option or activation through a browser/agent token. Do not relocate the
live Hub ledger or mount it over SMB to make this provider appear applicable.
The real Hub on TrueNAS still needs its separately reviewed staging transport.

## Explicit operator bindings

The operator supplies the immutable environment manifest and existing private
controller, checkout, fixed development ref, rootless Unix socket and output
directory. No command/path/socket/ref field is taken from an agent job. Each
job's independently reviewed commit must exactly match the ref before archive
creation or Docker effects. Controller bytes must be independently installed
outside the candidate checkout, current-UID-owned, single-link, non-writable by
group/others, in an existing private0700 directory, with the enrolled SHA256.
The provider loads those verified bytes, not a second read of the named file.

Composition requires a trusted isolated interpreter (`python -I`), trusted
package paths, and no candidate checkout directory on `sys.path`. Isolated mode
is necessary, not proof that operator-installed packages/imports are benign.
The operator remains responsible for trusted interpreter/package enrollment.
Do not run host controllers imported from candidate code. No package or engine
is installed, started or enrolled by this provider.

## Owned lifecycle

`invoke(attempt, guard)` calls the controller on a worker thread while retaining
the provider lock until it finishes, including repeated caller cancellation.
The worker's guard synchronously checks admission on the original event-loop
thread, not through a foreign-thread queue callback. Its bounded rendezvous
denies later effects on failure; an expired check cannot itself launch an effect.
The controller guards create/start/source admission. Accepted cleanup/stop is
still allowed when admission is lost. Loop and ledger ownership must remain
alive until accepted work drains; a cancelled future is not a stopped thread.

The controller retains its original durable engine/output intents before create
and enforces the existing rootless engine, exact profile and kernel caps. The
image's four dependency labels must also match the enrolled manifest. Neither
standalone CI's file lock nor this provider alone proves machine-wide writer
exclusion: the existing canonical WorkQueue and complete registry remain required.

## Private completion, no restart adoption

A keyword-only private controller callback captures detached exact source,
image, engine, container, profile, intent, timeout/failure and kernel evidence
while the original scope FD and admission locks are still held. It is called
only after exact retained-container/profile/PID0/exited-state verification and
the original pinned scope's descendant-empty check. Observer failure retains
HELD intents. Unknown create, invalid profile or missing original scope cannot
produce an accepted terminal observation.

The provider issues a private **process-local** receipt bound to the original
attempt. `verify_terminal` separately re-inspects the exact retained container
and engine/profile. It uses the captured original kernel observation; it never
reconstructs it from a later missing path. The advisory `result.json` is never
used as receipt authority. A timeout or cleanup failure with raw exit zero is
normalized to nonzero124, preserving the raw observation privately.

There is no durable receipt recovery, takeover, automatic retry or replay.
If the process restarts, loses its receipt, or cannot finish inspection, the
queue's original durable attempt stays reserved/held for explicit reconciliation.
Retained Docker metadata is not permission to adopt that attempt. The provider
does not delete containers or prune engines. Outside writers must remain excluded.

## Evidence and remaining acceptance

Tests cover real disposable Linux ownership/flocks/FDs with deliberately fake
Docker/Git effects, the actual controller-to-provider-to-owned-queue lifecycle,
retained-container mismatch, guard thread affinity, repeated cancellation,
timeout/raw-zero handling, pinned code/path/import rejection, and loading the
real controller in an isolated interpreter **without invocation**. They are not
real Docker execution, installed staging-host, remote NAS, reboot or restore proof.

The live Compose, endpoints, tokens, data and inactive/default entrypoints are
unchanged. Installation/re-enrollment of the changed controller and real bounded
host/remote pilot remain future acceptance steps, not implied by source tests.
