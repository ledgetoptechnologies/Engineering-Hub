# Development CI queue adapter — source integration, not activation

The local controller in [local-development-ci.md](local-development-ci.md) can
run reviewed, committed branch tests on the staging PC. Its local file lock does
not reserve that physical machine in Engineering Hub's shared queue. The adapter
work closes that coordination gap without changing the ordinary Hub entrypoint,
enabling a worker, or replacing the existing controller.

## Required composition boundary

- Use the existing `WorkQueue` and authoritative owned Hub ledger, with the
  canonical staging machine identity. Do not create a second machine identifier
  or independent scheduler to avoid a busy production reservation.
- Register a staging-only development-CI action through trusted composition.
  Agent requests retain the existing task/review/commit/artifact identity; they
  do not supply commands, executable paths, SSH destinations or a Docker socket.
- Retain the complete operator action registry so the queue can apply
  machine-wide, non-preemptive production precedence. Unknown same-machine
  production registrations must not silently turn into permission for staging.
- A private software-validation executor, not an agent bearer token, owns an
  admitted attempt. An immutable operator binding fixes the CI environment and
  reviewed provider. Normal development commits need not change Compose.
- Initial composition must reject existing pilot-executor policies rather than
  overwrite them. This is not yet a simultaneous multi-provider runtime; later
  composition must preserve both providers' admission and completion checks.

The owned-inactive Hub remains inactive. No environment variable, MCP request,
browser action or import may opt it into this execution adapter implicitly.

## Explicit source API

`DevelopmentCIManifest` binds the canonical machine, private engine, immutable
CI image, reviewed controller digest, four dependency digests, fixed profile and
bounded operation budget. It does not pin one development commit forever.
`WorkTarget.artifact_digest` binds this manifest digest, not just the cache image
ID; the independently reviewed source commit is a separate per-job identity.
An operator must validate and enroll that environment outside an agent request.

`QueuedDevelopmentCI` accepts an existing owned queue and an explicitly supplied
private provider. `run_once()` admits at most one eligible CI job; construction
does not invoke that provider, start a worker loop, create a host process, or
activate a transport. The provider's `invoke(attempt, admission_guard)` and
`verify_terminal(attempt, receipt_id)` are asynchronous operator-trusted methods.
The invocation must check the synchronous admission guard before each new host
effect. Verification must inspect the original attempt rather than accept
user-supplied JSON.

An optional concrete same-host provider is documented in
[development-ci-local-provider.md](development-ci-local-provider.md). It does
not supply the remote TrueNAS-to-staging transport or activate a live worker.

Tests can exercise this API with simulated providers, but that is not proof the
actual staging controller or private NAS connection has been enrolled for CI.

## Attempt and lifetime acceptance

Persist the job, fence, owner epoch, exact reviewed task/commit, CI environment
binding and original attempt identity before invoking a provider. A duplicate
request or restarted process must not silently replay an ambiguous attempt.

CI may run for 900 seconds while an individual queue lease lasts at most 600
seconds. An accepted attempt needs bounded renewal while its provider drains;
one initial claim cannot cover the whole operation. A missed/expired lease,
lost owner, changed review, pause or invalid registry must close admission to
later effects. It is not proof an already accepted effect stopped.

Caller cancellation, including repeated cancellation, must not force-cancel an
accepted host effect or release ownership while it remains active. Unknown or
nonterminal outcomes retain the machine for explicit reconciliation. Expiry,
process exit, a disappeared agent, a report file or a terminal-looking dict is
not capacity-release evidence.
If write ownership has already been lost, the adapter cannot safely mark the
job held itself. Read-only inspection may still show the retained `claimed`
record until the queue processes expiry; the physical reservation remains
bound. Neither that readable status nor an unexpired lease proves the old
executor is still authorized. A later owned process may observe the hold, but
this adapter supplies no takeover or reconciliation authority.

## Completion is a separate trust boundary

Provider invocation and terminal verification are different operations. Only
trusted, explicitly enrolled provider code may verify the exact original
attempt's source/image/engine/container identity and terminal kernel cgroup.
Public caller-supplied evidence or a structurally valid response is insufficient.
The local controller's existing advisory JSON is not an authenticated remote
receipt merely because it says `terminal_verified`.

A future host provider must prove the real binding and writer exclusion. No
remote provider, transport endpoint, credential, machine service, automatic
reconciliation or restart/restore authority is created by a source-only adapter.

## Verification and deployment reporting

Source tests must use disposable owned ledgers and simulated private providers,
covering production precedence, review changes, durable intent, proof mismatch,
renewal, lease loss, duplicate/old attempts and cancellation drain. Those tests
are not actual TrueNAS-to-staging execution acceptance.

Live activation still requires a published/verified image, accepted private
storage, reviewed operator configuration, enrolled host provider and a fresh
bounded pilot. Keep the current Compose and live services unchanged until one
consolidated handoff is accepted. Do not bypass the GitHub publication blocker.

## Durable host handoff (disabled by default)

`engineering_hub.development_ci_handoff` adds an opt-in protocol on top of the
same owned `WorkQueue`, `PilotOwner` and attempts table. Nothing enables it:
`DurableDevelopmentCIHandoff`, `CIHostHandoff`, `enroll_host` and
`reconcile_restored_journal` each refuse without explicit operator objects.

What is enforced in source:

- Host journal states `intent -> running -> terminal_held -> finalized_reserved`
  with triggers (no delete, immutable identity, `finalized_reserved` only from
  `terminal_held` with a 64-hex `hub_ack`). The exact schema/trigger SQL is
  validated on every open; a dropped, altered or extra trigger refuses.
- A start is admitted only with a fence above the journal, local hint and the
  EXTERNAL anchor, every earlier attempt `finalized_reserved`, and a Hub-signed
  release statement for exactly the host's latest attempt tuple (the Hub signs
  it only after its queue released that job).
- Current claim: every admission, and every effect step the runner guards,
  needs a fresh single-use host nonce (default validity 30 s, process-local)
  answered by the Hub's signed statement binding nonce, attempt, job, fence,
  generation and the live lease expiry read from the real `WorkQueue` at
  signing time. The host refuses unknown, used or stale nonces, any binding
  mismatch, and a lease not beyond host time plus the skew bound (default 5 s).
  A delayed or replayed signed start is refused. Per-effect confirmation needs
  a host->Hub `claim_confirmer`; the default refuses, so runs stay held.
- In-flight effects: each signed claim is recorded on the Hub ledger in the same
  transaction that re-checks the live claim and pause state
  (`hub_development_ci_effects`, bound to nonce, job, attempt and fence). A
  ledger trigger refuses any job transition other than to `held` while a mark
  is `in_flight`, so the job cannot be released or reassigned before the host
  reports terminal (`receipt_committed` marks them `reported`). Pause, revoke
  or lease loss only stop new signatures. There is no operator path to clear an
  unreported mark; an attempt whose host never reports stays held.
- Host nonces: at most `max_nonces` (default 64, 1..1024) live entries; expired
  entries are pruned by `time.monotonic()`, a full store refuses new challenges
  rather than evicting live ones, and each nonce is removed after one use
  whether confirmation succeeds or fails. Clock values, skew and leases must be
  finite (NaN and infinities refuse).
- Hub: `receipt_committed` binds the authenticated host inspection to the
  original claim/attempt. The authenticated host ack is recorded with its
  credential (`host_acked`) and re-verified before release; a ledger trigger
  refuses any `succeeded`/`failed` transition of a handoff job without that
  record. This stops old queue writers and raw SQL that do not also forge the
  record; normal WorkQueue APIs already need a context-local completion permit.
  SQL triggers cannot verify cryptography: a direct SQLite writer can forge a
  record that satisfies every trigger and then update the queue row itself.
  The Hub's own code re-verifies the recorded credential and never releases
  on a forged ack, but direct writers to the Hub ledger and the host journal
  must be excluded externally (file ownership/permissions, no other process
  holding the database), which this source does not provide.
- Enrollment, start, reconciliation and `scripts/staging_ci_engine.py stop` use
  the same validated engine `admission.lock` (current-UID 0700 directory,
  current-UID 0600 single-link inode, name still bound) as
  `scripts/local_development_ci.py`. While enrolled, engine stop opens the
  journal read-only and refuses unless every attempt is `finalized_reserved`,
  and fails closed on a missing/corrupt/mismatched journal, marker or anchor or
  a journal below the anchor. It also requires the same external anchor reader
  as admission (`stop(..., external_anchor=...)`); without one, including from
  the CLI today, every enrolled stop refuses. Unenrolled hosts keep the old
  behaviour. Any emergency halt for an enrolled host is a separate design that
  has not been reviewed and is not implemented here.

What is NOT supplied (each default refuses, so attempts stay held):

- An authenticated Hub<->host channel and keys (`RefusingAuthenticator`).
- An external non-rollback anchor (`RefusingExternalAnchor`). The local JSON
  anchor can be rolled back together with the journal and is only a hint; a
  coordinated local rollback is undetectable without the external anchor.
- A host->Hub claim confirmation channel (`RefusingClaimConfirmer`).
- A terminal verifier that re-inspects the original container and pinned
  cgroup (`RefusingTerminalVerifier`). A runner's observation is not proof.
- A host runner adapter around `scripts/local_development_ci.py`; that script
  refuses to run while enrolled, which also blocks the not-yet-wired adapter.
- Writer exclusion for older installed controllers (for example the installed
  f099 build or helpers copied from PR #147): they do not read
  `HANDOFF_ENROLLED.json`. They are excluded only while they hold the shared
  lock; they must be uninstalled or disabled before enrollment, which requires
  an explicit `LegacyControllersRemoved` operator attestation (not verified).
