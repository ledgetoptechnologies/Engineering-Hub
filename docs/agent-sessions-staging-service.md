# Isolated agent sessions staging service — source template

**Not deploy-ready.** This template is source packaging for a proposed first
isolated Hub, not a running or accepted deployment. It does not provide a
automatic initialization, router, Access policy, credentials, published image,
matching UI, or acceptance evidence. No host operation is authorized by this
file. Review the exact source revision and the deployment plan before any
separate operator approval.

The template requires an immutable artifact built from the reviewed
docker/Dockerfile.hub-control recipe, including its matching UI at
/app/dashboard. An arbitrary image repository or the legacy bus image is not
a substitute; verify the actual published image digest, recipe, source revision,
and UI identity before activation. The template explicitly overrides its entrypoint with
`/opt/venv/bin/python -m engineering_hub.owned_inactive_hub --config
/state/settings.json`. The hub-control recipe installs Python packages in
`/opt/venv`. The template disables its 8770 health check pending validation of
readiness for this selected paired-listener path. The selected process owns one Hub
and bus with fixed container listeners 8770 and 8771; it starts no executor or
work queue. It is not the private/public gateway composition. Health and
readiness require separately reviewed checks of the actual paired listeners.

## Inputs and isolation

Before activation, an operator must review and supply all of these independent
Compose interpolation inputs through a protected operator mechanism. Compose
`env_file` supplies the container environment, not interpolation values.

- `STAGING_IMAGE_REPOSITORY` and `STAGING_IMAGE_SHA256`: the reviewed repository
  name for the reviewed hub-control image and exactly 64 hexadecimal digest
  characters, respectively. The template
  requires both and constructs `repository@sha256:digest`; validate the full
  reference and published digest independently. Do not supply a tag such as
  `latest` as the release identity. Compose's required-variable expansion checks
  nonempty values, not digest syntax, artifact provenance, or UI compatibility.
- `STAGING_STATE_DIR`: an absolute, newly dedicated private local Linux directory
  unrelated to live or other staging data. It is mounted at `/state`. The Hub
  ledger parent must be owned by container UID 568 and have mode 0700; its
  ancestors must meet the source's ownership, permissions and no-symlink
  checks. Confirm host UID mapping, filesystem semantics and all file access
  against the source and accepted bootstrap. Never use a shared writable or
  network filesystem. The mount is writable for database, audit and lock state.
- `STAGING_CONFIG_FILE`: an existing new non-secret settings file, mounted
  read-only at `/state/settings.json`. Use strict deployment schema v1 with
  `schema_version`, `base_url`, `cloudflare_access` (issuer, audience,
  allowed_emails), `agent_allowed_hosts`, and `agent_allowed_ips` only. Its
  container parent selects `/state/hub.sqlite3` and the bus paths below. In the
  reviewed source, `Settings.from_env(deployment)` uses `deployment.hub_database`
  rather than the image default `HUB_DB=/var/lib/hub/hub.sqlite3`. Confirm that
  behavior in the exact source used to build the published artifact. The
  source file and state directory must be independently reviewed and must not
  refer to live data. Both bind sources must already exist; the template does
  not create them.
- `STAGING_ENV_FILE`: an existing protected staging-only environment file
  containing new, mutually distinct synthetic `HERMES_TOKEN` and `CODEX_TOKEN`
  and, if used, distinct `CLAUDE_TOKEN` and `ADMIN_TOKEN`. No token may begin
  case-insensitively with `ehi_`, `ehs_`, or `ehc_`. Do not commit, disclose,
  or copy live credentials. Review the file for unintended settings; its
  environment is passed to the service. Explicit Compose environment values
  override matching values in that file.
- `STAGING_AGENT_PORT` and `STAGING_BROWSER_PORT`: two distinct, unused host
  ports, different from live service ports, checked at the approved deployment
  window. Both publish only on `127.0.0.1`. Compose interpolation cannot verify
  distinctness, availability, firewall policy, or actual router reachability.

The state must already contain a newly initialized and owner-enrolled
`hub.sqlite3` and existing `agent-message-bus/messages.sqlite3`, `audit.jsonl`,
and `files/`. The existing CLI requires these and refuses to create or adopt an
unenrolled ledger. The cold-ledger enrollment utility is not the fresh-state
path. The separately reviewed initializer creates and enrolls a brand-new tree
using the existing owner and repository APIs:

```text
python -m engineering_hub.staging_state_init --new-state-dir ABSOLUTE_NEW_ROOT
```

This command creates state and must run only after approval of the exact new
location and image. It requires an existing private, owned, mode-0700 parent on
a supported local Linux filesystem and refuses an existing target, even an
empty directory. In a separate bootstrap container, mount only the approved
parent and choose a nonexistent child; an already mounted `/state` directory
is not a valid new target. Use the Hub control image's UID/GID 568 and verify
the resulting child is the later service's state mount. The initializer creates
no settings, tokens, sessions, listeners or workers. It preserves partial state
on failure; do not reset or retry that target. Never copy live state or use an
empty fallback to bypass enrollment.

The initializer and existing owned-inactive service tests passed 35 tests in
the cached isolated Linux validation image. Those tests used the explicit
ephemeral-filesystem test seam and UID 10001. They do not prove the deployment
image's UID 568, actual host filesystem, bind mappings or service startup. Prove
those selected-path conditions before activating the service.

The service has a dedicated Compose bridge network, no host network or Docker
socket, a read-only root, bounded `/tmp`, UID/GID 568, one CPU, 512 MiB
memory and equal total memory-plus-swap limit, 64 PIDs, all capabilities dropped,
no-new-privileges, no automatic restart, and a 1200-second stop grace period. Runtime support for those limits,
mount permissions and actual network isolation must be verified
on the approved host. No TEST/Engine path or live volume is selected.

A normal Compose stop or down can forcibly kill the process after the grace
period; 1200 seconds does not guarantee a drain. Before removing the service,
require a separately reviewed cooperative SIGTERM procedure and observe full
mutation drain and terminal confirmation. This template does not prove that
shutdown behavior.

## Human sign-in, then optional sessions

Initially leave `STAGING_SESSIONS_MODE` and `STAGING_SESSIONS_BRIDGE` unset;
they resolve to `disabled`. With the approved routing and Access configuration,
sign in as the authorized staging human and obtain only the authenticated
`actor` from `/api/hub/v1/auth/me`; do not record its cookie or CSRF token.
After review and explicit owner approval, configure the interpolation inputs
`STAGING_SESSIONS_MODE=staging`, `STAGING_SESSIONS_BRIDGE=enabled`,
`STAGING_SESSIONS_OWNER=<exact actor>`, and
`STAGING_SESSIONS_ORIGIN=<exact canonical base_url>`. The template selects
`/state/agent-sessions.sqlite3`, distinct from the Hub and bus paths; its
bridge lock is in the same private directory. The actor is sticky ownership,
not a label that transfers ownership. The bridge alone grants no Hub access:
the owner must explicitly grant a scoped, time-limited capability to an
approved installation. Restart or reconfiguration is a separate operator
operation after approval and lifecycle review.

## Gates before activation

- Accept the initializer on the selected image and actual private filesystem,
  with the approved new state path and UID 568 ownership, and separately
  provision protected synthetic credentials.
- Approve a private same-origin HTTPS router and Access policy. Route `/mcp`
  and `/api/agent-sessions/v1` to the agent listener without interactive
  redirects; route browser UI and `/api/hub/v1` to the browser listener with
  the Access assertion, cookies, Origin and CSRF intact. Review the remaining
  legacy bus paths and preserve canonical Host and Authorization. Verify the
  actual backend socket peer and firewall restrictions: forwarding headers do
  not establish the original client IP. Never expose the mapped ports publicly.
- Obtain an immutable backend image and matching UI build for the reviewed
  source, plus an independently verified compatible immutable rollback target.
  An older build that cannot read current actor/grant records is not a safe
  rollback. Disabling the bridge in a compatible build is containment, not a
  rollback of state.
- Accept a combined selected-path fixture covering direct paired listeners,
  one-origin routing, container port mapping, synthetic Access and state,
  session enrollment and exchange, approval and revocation, scoped bridge
  grants, denied paths and redirects, and absence of execution/queue access.
  Gateway/UDS fixture results do not establish this selected path.
- Approve the exact staging host, paths, ports, router and policy before any
  state changes or startup. Verify first-PC2 behavior and record only non-secret
  identifiers and outcomes. Retain databases and audit during rollback.

Docker Compose parsed the corrected template, including the stop grace period,
with interpolation, environment-file resolution and path resolution disabled,
and an explicit empty interpolation environment file. This static parse did not
validate deployment inputs or runtime behavior. The template has not been built,
started, deployed or runtime-tested. It does not establish native chat wake,
remote cancellation, or control of TEST/Engine.
