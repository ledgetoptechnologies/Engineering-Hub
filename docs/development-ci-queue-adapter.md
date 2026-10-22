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
user-supplied JSON. This source API does not supply a concrete host provider.

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

## Completion is a separate trust boundary

Provider invocation and terminal verification are different operations. Only
trusted, explicitly enrolled provider code may verify the exact original
attempt's source/image/engine/container identity and terminal kernel cgroup.
Public caller-supplied evidence or a structurally valid response is insufficient.
The local controller's existing advisory JSON is not an authenticated remote
receipt merely because it says `terminal_verified`.

A future host provider must prove the real binding and writer exclusion. No
such provider, transport endpoint, credential, machine service, automatic
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
