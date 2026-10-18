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
