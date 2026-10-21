# Explicit maintenance standby

This inert container entrypoint keeps the container alive for an operator's
container shell without starting Hub, MCP, a scheduler, database connections or
an owner. It does not create files, enroll, install, start children or activate
on boot. It imports only the standard library; the package initializer is inert.

It proves only its own mode. A running standby process does **not** prove that
other database writers are stopped, that a backup is valid, or that enrollment
has completed. Normal application startup is unchanged.

## Future operator override, after image publication

Save the original configuration first. Beau applies all TrueNAS changes.
This partial override is not a replacement Compose and is not ready to apply
until the image containing this module and the exact maintenance plan are verified:

```yaml
entrypoint: ["python", "-I", "-B", "-m", "engineering_hub.maintenance_standby", "--standby"]
command: []
healthcheck:
  disable: true
restart: "no"
```

Preserve UID568, the image and the existing single `/var/lib/hub` mount. Do not
widen mounts, permissions, ports or privileges. Override the complete entrypoint:
changing `command` alone appends arguments to the normal image entrypoint.
Disable the HTTP healthcheck while no HTTP listener exists; otherwise the
management UI may mark the container unavailable. No Hub UI/MCP is available
in standby. Explicit SIGTERM/SIGINT sets a lock-free local stop flag, checked by
an idle sleep loop every100ms; it checks no inbox, network, database or runtime.
There is no automatic timeout,
child, automatic restart, runtime fallback or enrollment on exit.

## Separate cold-enrollment prerequisites

Announce downtime, close admission and drain work. Stop and verify every writer,
including other containers, scripts, detached coordinators and other hosts.
Replacing this one container is not exclusion of those writers. Retain writer
inventory and operator attestations, labeled as attestations rather than proof
from this helper. Do not delete SQLite WAL/SHM/owner locks.

Take and verify a coherent whole-root cold backup covering the Hub ledger, bus,
files, notes and audit. The Hub-only backup helper is not a complete backup of
that root. Use a separately reviewed destination on the actual persistent
writable volume, exclude the backup itself and verify copies without opening
original sidecars. This document deliberately supplies no unverified tar command.

Only then use the separately reviewed `pilot_enrollment inspect` and `apply`
workflow in [offline-pilot-enrollment.md](offline-pilot-enrollment.md). Its two
flags are operator attestations. Inspect output is not an instruction to apply.
On refusal, partial state or ambiguity, preserve artifacts and stop: no blind
retry, automatic restore, ordinary-startup fallback or owner-lock removal.

After approved enrollment, use the exact independently reviewed
`unified_pilot_startup serve` selector and protected profile, not a blind restore
of the ordinary application entrypoint. Reestablish the actual ledger identity,
agent connections and UI before admission. The final operator-specific settings
and startup invocation remain a separate handoff; this helper cannot create them.

## Acceptance limits

Pure tests cover explicit argument gating, fixed status, mock sleep/signal
handling, restoration on failure and no helper file opens. They do not prove
Linux PID1 delivery, TrueNAS shell availability, UID/ZFS permissions, writer
exclusion, backup, enrollment, app startup or reboot recovery. Actual image
packaging and container acceptance are required before the operator window.

The exact helper source (SHA256
`928f3239310e9c69503ffa924ce06c61babfa08ba1e7c939c0928b5010a831d8`)
also passed two disposable Python3.12 source-process checks on staging Linux:
SIGTERM and SIGINT each returned0 with fixed stopped status and an unchanged
empty working directory. These processes were ordinary unprivileged children,
not container PID1; they touched no Hub ledger, executor, proxy or application.
