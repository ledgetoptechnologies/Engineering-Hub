# Disposable two-image rollback verification

The manual `Verify immutable Hub rollback` workflow runs only on `main`. It
uses the repository's existing `GITHUB_TOKEN` with `contents: read` and
`packages: read`, and Docker's official login action. It neither changes package
visibility nor grants access to another principal. A registry denial fails the
run; there is no alternate image or mutable-tag fallback. The retired bus
`verify-published` workflow is separate and is not used.

Inputs select two full SHA256 digests from the fixed
`ghcr.io/ledgetoptechnologies/engineering-hub-control` package and two full Git
commits. The harness verifies each local image's ID, RepoDigest, commit label
and configured UID/GID568 before creating any fixture. All containers use the
resolved image IDs with pull disabled. Their network is `none`, ports are not
published, root filesystem is read-only, capabilities are dropped, and
no-new-privileges/private cgroup namespace are required. Each has 1CPU,
512MiB memory/zero additional swap,64PIDs,32MiB noexec temporary storage and
128KiB rotated logs. Only uniquely labelled disposable named volumes are used;
no host bind, source credentials or Docker socket enters a container.

The baseline creates and claims a fixture task, then makes a coherent SQLite
backup. It must terminate before the candidate opens that same state. The
candidate verifies the old task and creates/claims a distinct task. The
candidate must terminate before the old image opens state again. Health build
identity and real HTTP authentication denials are checked by each app probe.

Choose the proof explicitly; a failed direct attempt never automatically turns
into a successful backup-restore claim:

* `direct`: the baseline image reads both the original task and the distinct
  candidate-written task, including their revisions/owner/status. This proves
  compatibility of this candidate's exercised writes with the baseline.
* `backup-restore`: a separate volume receives the exact pre-upgrade backup,
  with SHA256/size and SQLite integrity checked. The baseline reads the original
  task and verifies that the candidate task is absent. This proves recovery of
  the original state while deliberately discarding candidate writes; it does
  **not** prove direct schema rollback.

Shutdown issues SIGTERM at most once per captured container ID and observes
terminal PID0/non-OOM/normal exit. There is no `docker stop` timeout-to-KILL,
forced remove, retry of an unknown signal acknowledgement, or second writer
while the first writer is uncertain. The main attempt has480seconds; cleanup
has a separate120second budget. Each CLI output is capped at256KiB and its
child is reaped on timeout. Unknown CLI outcomes are inspected by original
owned names, never resubmitted. Failed container cleanup leaves volumes held.

The only uploaded artifact is the bounded public JSON receipt: selected image
identities, proof mode/outcome and disposable ownership/cleanup observations.
No image layers, database/backup bytes, credentials, or private container logs
are uploaded. A failed or held result exits nonzero. Missing registry access
can prevent this evidence; it does not justify substituting another baseline.

Receipts retain the entered and completed fixed step names, the first failed
phase, and allowlisted error categories/codes. A known CLI failure may include
only its bounded integer exit code. Exception messages, command strings and raw
stdout/stderr are never published. Cleanup failures retain their own safe phase
without replacing the original verification failure. Exact target-scoped Docker
missing-volume/container messages are accepted only for an explicit absence
check with exit1 and empty stdout. Permission, timeout, mixed, unscoped or
foreign-target diagnostics remain unknown and cannot authorize creation.

The initial direct workflow run40269086431 remains failed with an unknown
failure phase; its original receipt was insufficient to diagnose the cause.
A subsequent read-only local probe reproduced a lower-case Docker missing-volume
variant that the earlier reused helper rejected. The scoped compatibility fix
and improved diagnostics do not retroactively establish that run's cause or
constitute a successful rollback run.

This is synthetic app-state evidence. It does not cover live NAS state,
protected-owner enrollment/epoch restoration, machine reboot, live human OAuth,
native agent wake, or arbitrary migrations/data shapes. Container resource
configuration is inspected; it is not a real staging kernel/quota acceptance
test. Tests of this harness use mocks and are not actual rollback proof.
