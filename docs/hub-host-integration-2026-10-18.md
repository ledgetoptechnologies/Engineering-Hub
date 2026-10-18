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
