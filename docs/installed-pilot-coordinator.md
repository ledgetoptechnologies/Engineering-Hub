# Explicit colocated TEST coordinator

`hub_container_executor.installed_coordinator` selects the existing private
queued-pilot service. It adds no scheduler and does not change ordinary Hub or
unified MCP startup. Importing the module starts nothing. Source tests are not
an installed pilot, host approval or a network connection to TrueNAS.

## Separate validation and serving

The explicit module operations are `validate --config <absolute-path>` and
`serve --config <absolute-path>`. Both require Linux PID1, real/effective
UID/GID10001, and a canonical regular single-link configuration file owned by
that UID, mode0600 on a read-only mount. The file must remain stable while read
and cannot sit beneath the writable database parent. No paths are provisioned
or repaired. A valid file or canonical JSON is not approval of its contents.

`validate` reads only the configuration and validates its data/model shape.
It does not open secrets, sockets, providers or the database, contact an
identity provider, or establish host readiness. It does not validate an actual
supervisor's drain behavior. `serve` separately resolves four distinct TEST
credentials and calls the owned queue service. A missing required core Unix
client API is rejected before secrets or owner state are touched. That check
is compatibility evidence, not authentication of the core artifact.

The closed `engineering-hub.installed-coordinator.v1` configuration contains:

- `manifest`: the complete existing canonical pilot manifest.
- `database_path`, `coordinator_socket_path`, `executor_socket_path`, and
  `secret_root`: absolute paths with pairwise-disjoint database parent, socket
  parents and secret root. No one of these roots may contain another.
- `agent_port`: an explicit loopback-only port1024–65535, excluding existing
  bus/Hub ports8765,8770,8771. Both loopback Host forms—with and without the
  configured port—are explicitly allowed; other ports are not.
- `machine_id`: exactly `staging-pc`; `action_key`: exactly
  `engineering-hub:container-pilot`; `allowed_actors`: a unique nonempty subset
  of `codex`, `hermes`, `claude`.
- `public_auth`: existing Cloudflare Access configuration fields `base_url`,
  `cloudflare_issuer`, `cloudflare_audience`, `cloudflare_allowed_emails`.
  This configures no Access application/policy and does not reuse or alter Ops.
- `secret_bindings`: exactly `codex`, `hermes`, `claude`, `executor`, each with
  `slot_id`, `version`, `digest`. Slots must be TEST slots; both slot/version
  identities and content digests must be distinct. No raw token is accepted in
  configuration, and no admin token is enabled.
- `supervisor_stop_grace_seconds`: at least the existing1120-second requirement.
  This assertion does not install or verify a supervisor.

## Explicit manual-session selector

The installed CLI remains closed by default. Bare `serve --config <absolute-path>`
fails before resolving TEST secrets because JSON and CLI arguments cannot create
the trusted reconciliation and owner-acceptance callbacks. A bounded TEST
session must be explicitly selected with both protected files:

```text
serve --config <coordinator.json> --session-profile <pilot.json> \
      --session-enrollment <manual-session.json>
```

`--session-profile` uses the existing fixed `engineering-hub.unified-pilot.v1`
profile. `--session-enrollment` uses the closed
`engineering-hub.manual-staging-session.v1` data format. The selector accepts
absolute paths only, reads each file through the existing no-follow,
stable-identity protected-file reader, and resolves no module, callback, provider,
or approval flag from those files. The profile manifest, machine and shutdown
grace must match the installed TEST coordinator. The selected actors and task IDs
further narrow the coordinator's exact-candidate veto. Current-UID TEST
credential slots remain separate from the TLS credential captured for the
identity observer; credential reuse is rejected.

The fixed `TrustedQueuedSessionComposition` binds a fresh owner/process challenge,
current executor identity observer and owner-only manual acceptance broker to
the same manifest and database. Admission starts pending/closed. Human
acceptance only opens this bounded admission gate; repository task ownership,
independent review, queue fencing, exact candidate identity and current
reconciliation checks remain required before effects. The accepted composition
is closed only after the owned service drains accepted work, including
cancellation. Restart or expiry requires a fresh enrollment and owner decision.
The report's inventory/writer attestations remain operator claims, not automatic
proof of host state.

## Existing authority remains mandatory

The concrete additional target policy only permits an allowed actor, this action,
staging, and the exact manifest candidate commit/config digest. It is a veto,
not a substitute for the existing repository task ownership, independent review,
queue fencing, physical-machine reservation, manifest/build enrollment or current
host approval. The existing event-driven runtime and cancellation/drain ownership
remain in control. No automatic recovery, replay, lease-based reassignment or
production authority is introduced.

`ContainerPilotClient.for_private_unix` creates a fresh client using the existing
private path checks: current-UID mode0700 parent, trusted no-follow ancestors and
mode0600 socket. It has no TCP fallback, environment proxy, retry, redirect or
caller callback. Existing bearer, response identity and whole-operation deadline
checks remain unchanged. These filesystem checks do not authenticate a kernel
peer or isolate equally privileged root/same-UID mutation after validation.

The coordinator must not receive an Engine socket, issuer key, production bus
database or production tokens. Its private browser endpoint is not the host
executor endpoint; use the existing separately owned browser gateway if approved.
This pilot uses an ordinary private Hub core, not a second production message bus.

## Packaging and live acceptance

Use independently approved immutable core and optional wheel artifacts containing
both this module and `ContainerPilotClient.for_private_unix`, plus the current
observation/HTTPS transport/reconciliation core modules. The recipe requires an
explicit independently approved immutable core image and has no default. Its
former `f40ada02...` core (main `cb7b790...`) predates current observations and
is no longer compatible. Offline installed-module imports now fail the image
build for that mismatch, without starting a service. Current wheel/archive
checks require both coordinator and launch-supervisor modules. The earlier
locally tested executor wheel/image did not contain this coordinator module;
the changed source does not update those old artifacts. Rebuild and run
`scripts/hub_executor_image_smoke.py` against the exact new image/source to check
installed paths, source hashes, client compatibility and fixed entrypoint.
Neither version strings nor this offline import check establish installed
coordinator readiness. Do not mount a mutable checkout as a substitute or
silently change the executor's fixed entrypoint.

A colocated Unix connection is not remote reachability. TrueNAS-to-staging still
needs an explicitly reviewed authenticated forwarding boundary with correct
filesystem access. No such forwarder, public route, service, startup registration,
permission change or PC-reboot recovery is installed here.

The current [pilot preparation handoff](staging-pilot-preparation-2026-10-19.md)
records the stopped runtime, independent issuer/mapping requirements and aggregate
resource/lifetime conflict. Actual direct receipts, image enrollment, resource
enforcement, queue-to-real-Docker deploy/health/rollback and restart observations
remain required. Synthetic lifecycle or Engine fixtures do not establish them.
