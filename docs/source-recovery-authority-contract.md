# Source deposit recovery: authority still required

This proposal defines the missing authority; it does not add an epoch, approve
an operator procedure, resolve a held request, or enable the observation route.
The current bearer authenticates access to the configured endpoint; it does not
bind a response to the original live process, host, ledger generation, or an
anti-rollback authority. A random UUID persisted in the same SQLite database is
not such an authority: a copied or restored database can reproduce it.

## Owner admission record

Before allowing any source deposit, a configured human owner must establish and
record an exact admission for one worker process, transport peer, worker root,
ledger generation and target host. The existing TEST/Engine owner must first
stop or drain every prior authorized writer, preserve evidence of its original
operations, account for external writers and the original storage, and inspect
the exact current peer and storage under independently protected operator
custody. A fresh process-bound challenge must bind the original Hub operation,
the admitted process and peer, ledger incarnation, machine/boot identity, and
the owner's decision; a reused network address, CA, bearer, device/inode pair,
or restored SQLite UUID is insufficient by itself. Admission is not execution
approval and does not replace independent task review.

How that operator verifies the host and stores an unforgeable, rollback-resistant
generation is **unresolved**. A separate monotonic service is one possibility,
not an established requirement or existing capability. A trusted operator with
an independently retained audit record may be part of a first-release process,
but the implementation must specify its exact evidence, freshness and recovery
rules before it is relied on. No profile or response can self-enroll authority.

The existing `PilotOwner` and `StartupReconciliation` machinery provides a
local process/FD/owner-epoch admission for the separate queued container pilot.
It does not authenticate this modular worker's journal or exclude legacy and
external writers. Reusing that epoch as a modular source generation without an
independently checked peer and effect boundary would be an invalid authority
upgrade. The existing first-release model permits an explicitly trusted operator
to reconcile stopped writers and actual effects; it does not assert unattended
rollback detection or require inventing a third service.

## Original attempt and worker journal

The Hub must atomically persist the original source UUID, project, archive and
full verified Git binding, exact reviewed task/profile and admitted worker
generation, plus its request key and session/grant provenance, before any
network effect. The receiver must reject a stale or unauthenticated generation
before effect and atomically associate the same original identity with a durable
attempt state. It must never accept a caller-selected new generation as proof.

File and journal writes need a tested crash protocol: source creation and fsync,
directory persistence, journal commit and acknowledgement can fail separately.
An orphan file is ambiguous, not a rejection. A durable non-evicting attempt
record or tombstone must survive source garbage collection and distinguish
accepted, rejected-before-effect, and uncertain states without overwriting an
earlier fact. Bound storage requires explicit backpressure and operator recovery,
not silent eviction. Same request with different binding is a conflict.

One concrete sequence to implement after the authority exists is: persist a
bounded immutable attempt identity before opening the destination; write a new
nofollow, exclusive file and fsync the file and containing directory; atomically
bind its incarnation, exact source binding and original admitted generation in
the worker journal; then return an authenticated receipt. Every transition must
be idempotent for the *same* operation while conflicting identities fail closed.
An attempt left before the receipt is not automatically no-effect even if the
row or file is absent. A prior compatible `v1` request and its historic source
must not be rewritten or adopted as a `v2` attempt.

## Recovery and revocation

Reconciliation inspects only the original source and attempt on the exact
admitted authority. The existing observation endpoint can provide a matching
current row and tree evidence, but it cannot prove an original attempt, epoch,
or absence; `unknown` and missing/expired rows always remain held. An authenticated
positive observation alone does not authorize queue dispatch or hold release.

To conclude no effect, the authority must prove the original sender and every
receiver/writer are quiescent, the original generation has been drained, and
its durable attempt record rules out a commit including orphan and in-flight
cases. Revocation must order against receiver admission and drain: a Hub-side
policy check before an await cannot fence a later remote effect. Changes to
owner enrollment, session, profile, task, host identity, epoch, or storage
restore must stop new admission and preserve unresolved attempts. No network
await may occur while holding the Hub or session SQLite writer.

## Qualification before enabling

Independent review and real admitted-host tests must cover both orders of
revocation versus admission, delayed in-flight POST and drain, lost ACK, crash
before/after file fsync, directory fsync and journal commit, cleanup/expiry,
source ID reuse, corrupt bindings, peer substitution, worker restart, database
restore/rollback, old/new version interaction, bounded retention exhaustion,
and exact-original no-repost recovery. Tests must preserve and inspect actual
receipts and identities. Local synthetic tests cannot establish host custody,
power-loss behavior, an external fence, or staging acceptance.
