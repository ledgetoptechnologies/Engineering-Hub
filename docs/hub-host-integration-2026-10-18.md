# Optional host executor integration — 2026-10-18

## Frozen Linux evidence

Base: main `c720f7a82c70230e267a369077211749c30e9360`.
The optional host source was imported from reviewed overlay
`12abc7c2c8d9e22d9dac3c9ce5cc8b6930b34625`, then narrowly corrected for the
published image's database default and trusted role-specific health identity.

Frozen validation image index:
`sha256:1e2f5def8b2333067052509e781bdf607b39224e88925a3f6cbfbf6ad58bc5a0`.

- Full Linux Python suite: **2337 passed, 1 skipped, 88.20 seconds**, exit 0.
- Focused health-script, backend, journal and import-boundary tests:
  **138 passed, 1 skipped, 5.41 seconds**, exit 0, with
  `-W error::ResourceWarning`.
- Full suite emitted the existing Starlette deprecation and an unclosed asyncio
  event-loop warning; the focused strict-resource run did not reproduce the
  latter. This does not establish that the full-suite warning is resolved.
- Independent review found no blocker in the exact database-default exception,
  trusted candidate/rollback source binding, actual compiled health literal or
  import-inertness test. The Linux import-inertness check passed in both runs.

Both test containers used no network, no host mounts, no Docker socket, a
read-only root, dropped capabilities, no-new-privileges, private cgroup namespace,
two CPU/2 GiB/256-PID limits and bounded disposable tmpfs storage. The full suite
ran `python -m pytest -q -W default`; the focused run changed the warning policy
as stated above. Both used `--basetemp /tmp/hub-validation` and an ephemeral
pytest cache.

## What this proves and does not prove

The optional source integrates with current main without changing the live bus,
Hub runtime source, production images, Compose or publication workflows. Only
the validation Dockerfile copies the optional package. Imports do not open a
database, create a client, start a listener or invoke a subprocess.

Health probes bind exact `ok: true`, service and source commit to the verified
image selection, using the rollback commit when checking rollback. Durable
journal tests cover committed intent/completion and process exit/reopen, not
every possible storage fault or an interrupted SQLite rollback transaction.

This is **not actual Docker Engine execution acceptance**. The acceptance CLI
remains disabled. Trusted enrollment, executable identity measurement, private
state/baseline provisioning, real executor packaging/composition and actual
success/failure/rollback/restart scenarios remain necessary. No NAS, live bus,
production deployment, credential or access-policy operation occurred here.
The actual NAS install, LAN isolation, fresh human sign-in and app/reboot
acceptance remain operator-dependent gates. The full goal is not complete.

Any later local-provider changes require a new frozen image and their own test
evidence; the image above does not contain those subsequent additions.

## Read-only local providers

The next snapshot adds `PrivateContentStore` and
`ImmutableTestSecretResolver`, with explicit private filesystem provisioning,
digest verification, strict bounded reads, immutable version bindings and no
ambient credentials or provenance approval. Independent read-only review found
no blocker. Build verification still refuses a missing trusted verifier.

Snapshot including the actual fork test:
`sha256:5c1fe9d16fb87abfccce3abeeb0b26f9183037fa6fb9a6d4394fdceea6f25d5f`.
Full Linux suite in this snapshot: **2384 passed, 3 skipped, 90.40 seconds**,
exit 0. The warnings are the same Starlette and event-loop warnings noted
above, plus the deliberate multithreaded fork deprecation described below.
All three skipped ownership checks then passed in a separate invocation of
this exact snapshot (**3 passed, 0.33 seconds**) with the isolated `CHOWN`
profile described below. No case was skipped across the combined runs.
Focused provider/import tests: **48 passed, 2 skipped, 1.05 seconds**, exit 0,
with resource warnings treated as errors. The deliberate multithreaded fork
produces Python's fork deprecation warning; the test confirms child read denial
and child cleanup while the inherited mutex is locked, then parent usability.

The two skipped provider owner-change checks passed separately (**2 passed,
0.29 seconds**) in snapshot
`sha256:bbd882d20168a0e5afc1d6ebe96a212f2ca6db1f0a30eb50065e2b96a7023b35`,
before the fork-test-only addition. The original journal owner check also passed
separately (**1 passed, 0.30 seconds**) in the original integration snapshot.
These three cases used container UID 0 with only `CHOWN` added to otherwise
dropped capabilities, no host mounts/socket/network and disposable tmpfs.
The first journal owner-check invocation omitted the test fixture tmpfs and
failed during conftest setup; the corrected invocation supplied it and passed.
These isolated tests do not grant the executor a runtime chown capability.

## Explicit build enrollment and bounded SQLite copying

`OperatorEnrolledBuildVerifier` now provides an exact immutable allowlist from
trusted local composition, bound to manifest digest, role, exact record digest,
builder and invocation. It never derives approval from content-store discovery,
record self-claims or agent input. This is an operator trust anchor, not a CI
signature verifier; no real build was enrolled by these synthetic tests.

State copying now accepts an optional absolute cooperative monotonic deadline.
Checks surround I/O, SQLite queries/VM progress, backup-page callbacks, bounded
hashing and durability operations. A concrete SQLite-error cleanup bug was fixed:
previously a sibling exception handler translated the error without removing
the newly created partial destination. Cleanup now covers SQL errors and does
not unlink an unrelated replacement inode. Connection closure also covers a
destination-open failure. None of this forcibly cancels a blocked disk call.

Independent source reviews accepted both changes. Frozen Linux snapshot
`sha256:a1951bf7c95bb50eed4a7c2ecd013113d083f44e1e103b54f9dfb92bdaf39b1a`:
**2425 passed, 3 skipped, 86.46 seconds**, exit 0; same three warning classes as
the prior full run. Focused enrollment/copy/import tests with resource warnings
as errors: **45 passed, 1.00 seconds**. The three skipped ownership cases are
unchanged from the separately verified owner-guard tests described above.

One subsequent test-only addition exercises a real long-running recursive
SQLite query: the installed VM progress callback interrupts it at its deadline,
without mocking the clock, connection or query. Snapshot
`sha256:bbdbb8c7e73a2899c85740d5aa64564ab95d1c782891e12bd704c8d0f20d1dd0`
passed all **46 focused tests in 1.15 seconds**, with resource warnings as errors.
The full-suite count above predates this one extra test.

The baseline/state-generation provider and real daemon pilot remain incomplete.
No host socket, NAS, production state, agent credential or runtime was changed.

## Private state provider and crash recovery

`FrozenSQLiteStateProvisioner` now supplies real Linux filesystem/SQLite copies
under an explicitly provisioned UID/GID10001. Its trusted manifest and external
baseline provider remain mandatory; it does not invent healthy-baseline proof.
It pins separate private generation/reservation roots, baseline inventory and
backup identities, uses durable exclusive intents/completions and checks quotas
and deadlines. Frozen backups require a quiescent rollback-format SQLite file,
without WAL/SHM/journal companions. Baseline auxiliary files may be empty.

Independent review found and resolved an actual workflow defect: admitting a
candidate could consume the last slot or bytes needed for mandatory rollback.
Candidate admission now checks two new generation slots and two backup-sized
allowances before intent. An outstanding same-run rollback reservation survives
reopening and cannot be consumed by another candidate or unrelated rollback.
The first Linux run also exposed 35 fixture errors (a model was treated as a
dictionary); these were repaired rather than reported as passing coverage.

Corrected provider snapshot
`sha256:c3ff1385c423213342d043a7704c237eb6859d2cf8bf8fa00c01104f3d944878`:
**37 passed, 0.76 seconds**, exit 0. Independent review accepted the quota fix.

Final snapshot including an actual crash test and import boundary:
`sha256:7c31d8204f60e3cecbb9d159df412ceac1bda5512462d3acfd9e0013f75274d4`.
Focused tests: **39 passed, 1.49 seconds**, exit 0, with resource warnings treated
as errors. A freshly constructed child exits with `os._exit(23)` after durable
intent; the parent reopens and proves original/new operations hold without copy,
artifact deletion or changed baseline bytes. This is real process/file recovery,
not machine reboot, actual Docker effects or success promotion after ambiguity.

Full suite in this same final snapshot: **2464 passed, 3 skipped, 91.91 seconds**,
exit 0, with the previously documented Starlette, event-loop and deliberate-fork
warning classes. The three skipped owner-change checks passed separately in the
same snapshot (**3 passed, 0.31 seconds**) using the disposable UID0/CHOWN-only
profile; the state-provider tests themselves ran as real UID/GID10001.

Limits remain deliberate: admission accounting is not a physical OS disk quota;
runtime application growth can consume disk space. A successful candidate keeps
its rollback reservation, so continuous staging campaigns need a separately
reviewed success-release/retention lifecycle. OwnedPilotService's inode-bound
state is not a supported target clone. The actual runtime identity/launcher,
baseline enrollment and real daemon success/rollback pilot remain unproven.
