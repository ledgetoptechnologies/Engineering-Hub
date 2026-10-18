# Private Hub control deployment — operator-gated

This standalone Compose profile is deployment configuration, **not evidence of a
running service or release acceptance**. It does not activate anything by being
committed. The working message bus, its images, volumes, port 8765, MCP endpoint
and clients remain unchanged. Do not combine this file with either bus Compose
file. The control image is built from `docker/Dockerfile.hub-control` and serves
its compiled dashboard plus the independent Hub API/MCP on container port 8770.

## Operator-only split ingress update (candidate, not deployed)

Beau owns all TrueNAS administration. Agents prepare/review source, images and
instructions; they must not inspect or change NAS management, containers, ACLs,
mounts or services on his behalf without new explicit authorization. Beau
confirmed the existing cloudflared connector runs on this NAS with host networking.
The NAS clock must be synchronized: Access assertions use strict issued-at,
not-before and expiry checks. A clock error can deny sign-in; verify time sync
rather than bypassing JWT validation or extending accepted token lifetimes.

The candidate `--split-ingress` entry point binds the agent front door on the
existing Hub port8770 and the browser front door **only** on `127.0.0.1:8771`.
It binds both sockets before constructing the core, creates one repository,
session store, MCP manager, event feed and optional pilot owner, and runs the core
lifespan once. Each HTTP listener has its own role guard and no independent
lifespan or implicit proxy trust. Losing either listener stops its peer before
the shared core shuts down. HTTP draining has a30second allowance; the declared
1200second supervisor grace also covers the owned-pilot contract. No executor is
activated by selecting split ingress.

Only after this source is merged and the matching main-built image is published:

1. Privately save the current Hub configuration and protect its dedicated state
   with the established snapshot/SQLite backup procedure. Keep agent token values
   private. Do not change or restore the existing message bus's data or settings.
2. Update **only** the Hub app using the reviewed `compose.hub-control.yaml`:
   retain its current three real agent tokens and dedicated state path, pull the
   tested main image, add the split-ingress command, and configure Hub-specific
   Cloudflare Access identity. The application AUD is public configuration, not
   an authentication secret. Both8770 and8771 must be available. Do not set the
   core to standalone `browser` or `agents` mode: the split runner's one core
   remains `combined` behind two separately guarded adapters.
3. Change **only** the Hub tunnel route's service URL to
   `http://127.0.0.1:8771`. This loopback works because Beau confirmed both
   applications use host networking on the same NAS. Leave the other route(s),
   tunnel token, Ops policies and legacy bus8765 untouched.
4. Verify from another LAN machine that8770's dashboard/assets/auth routes deny
   access,8771 cannot be reached, and the approved agent clients can still call
   Hub MCP/API with their existing private token helpers. Also verify the old
   bus remains reachable. Through public HTTPS, verify no-cookie Access challenge,
   authorized Hub sign-in, shared task visibility, and logout. A successful
   health check is not evidence that human sign-in or agent readiness works.

The tunnel should retain the canonical public Host header. Do not use proxy
forwarded headers as an authentication or source-IP bypass. Existing exact agent
IP restrictions remain unchanged; investigate an unexpected source rather than
silently opening all LAN addresses. There is no need to add Nginx for this layout.

If the update fails, stop only the Hub candidate and use the saved, reviewed
image/configuration and compatible dedicated-state recovery procedure. Keep the
public Hub route protected or unavailable during rollback; do not knowingly
re-expose the old combined dashboard on the LAN to declare recovery complete.
Report any return to combined ingress as a security limitation, not successful
tunnel-only acceptance. Actual NAS installation, network isolation, browser
sign-in, restart and reboot acceptance remain operator verification steps.

## Preconditions and private storage

### Tunnel-only browser ingress: source capability and acceptance gate

The initial host-network Compose serves one combined listener. A Cloudflare
Access application at the public hostname does **not** hide that listener's
dashboard from the LAN. Do not call the deployment tunnel-only until direct LAN
requests to the dashboard, assets and human-auth routes are denied, including
requests with the public Host header and forged forwarded/identity headers.

The source supports explicit `HUB_INGRESS_MODE` values:

- `combined` is the compatibility default; it does not provide ingress isolation.
- `agents` admits only health and existing bearer-authenticated Hub API/MCP
  routes, never the dashboard, assets or human-auth routes. Existing source-IP
  restrictions and human-only action restrictions still apply.
- `browser` requires complete Cloudflare Access configuration at startup and a
  valid Hub-specific assertion on every non-health browser request, including
  assets and sign-in. Agent Authorization headers and MCP are denied. Existing
  local sessions, identity pins, Origin and CSRF checks remain required.

`HUB_AGENT_ALLOWED_HOSTS` is an optional comma-separated list of exact MCP
`host[:port]` values, including bracketed IPv6 where needed. It does not disable
DNS-rebinding protection or make a Host header evidence of tunnel transit.
The entry point disables Uvicorn forwarded-header trust; agent source-IP checks
use the actual connected peer rather than user-supplied proxy claims.

Network isolation remains an independent deployment requirement: the full
browser origin must be reachable only by the verified tunnel connector/private
network, while only the agent front door is available on the approved LAN.
Do not publish the browser listener on all interfaces or assume that a remote
or bridge-networked connector can reach the NAS host's loopback address.

Two independent app instances sharing local ZFS SQLite can be evaluated for the
current unowned release, but are **not** the permanent autonomous-pilot design:
an owner-enrolled ledger intentionally refuses an unrelated second process.
Browser mode rejects supplied execution components. Never weaken PilotOwner
guards to accommodate a second frontend. An owned pilot needs a single-owner
core with authenticated frontends and an explicitly tested single-owner,
multi-listener lifecycle before activation. The candidate split runner described
above implements that lifecycle; it does not make two independent processes safe.

Acceptance must cover private browser reachability, agent token/IP access, all
three identities, cross-frontend task/session/native-event consistency, proper
disconnect cleanup, and unchanged legacy MCP on 8765. Source tests are not
TrueNAS, tunnel, signed-in-browser, restart or reboot acceptance. Until the
topology is verified, retain the existing live configuration and report the
remaining LAN exposure explicitly.

- Create `/mnt/Plugins/App_Data/Agent_MCP/engineering-hub` on the NAS's local
  ZFS storage, under the established MCP parent but never sharing its database.
  This follows the owner-supplied `Current-MCP.yaml` mount hierarchy. Do not use
  an SMB/NFS client mount or mount the whole parent into Hub.
- Only this new child directory needs UID/GID `568:568` and owner rwx access
  (or restrictive equivalent TrueNAS ACLs with administrator recovery access).
  Verify inherited ACLs. Never recursively chown the parent, pool, or bus data.
  `create_host_path: false` intentionally rejects a missing directory.
- Hub stores its SQLite database, WAL and session tables in `/var/lib/hub`.
  No Docker socket, host source tree, SMB mount or bus database is exposed.
- Supply the existing separate Hermes, Codex and Claude bearer values through
  the operator's protected deployment environment. Never paste them into this
  file, source control, chat, command arguments, examples or logs. The operator
  must protect Docker/TrueNAS administrative access: container environment values
  remain visible to administrators. Do not print `docker compose config` or
  `docker inspect` output containing expanded secrets.

## Image and configuration gate

The operator Compose uses
`ghcr.io/ledgetoptechnologies/engineering-hub-control:latest` with
`pull_policy: always`, per Beau's main-built moving-image requirement. A moving
reference does not enforce immutable image syntax; exact registry digests and
source revisions belong in release/rollback evidence, not this default Compose.
The separate `.github/workflows/hub-control.yml` verifies the source, builds from
that exact main commit, checks private authenticated registry read and isolated
startup, then promotes the tested digest to `latest` and reads it back. PRs
validate only and cannot publish. A plain restart does not pull an updated image.

The package stays private. Configure authenticated GHCR pulls in TrueNAS using
an authorized GitHub account and a PAT (classic) with `read:packages`, including
any required organization SSO authorization. Existing registry credentials may
work if they can read this separate package. Never put the registry PAT in Git,
chat or Compose. CI's ephemeral GitHub token is not an installation credential.
Authenticated CI access does not prove the NAS's own credentials are enrolled.

Only replace the three existing agent-token placeholders locally in the private
TrueNAS application configuration. Leave the human-auth fields empty until the
separate authentication setup is approved and verified. No hidden env file or
source build on TrueNAS is required.

The profile limits the service to a read-only non-root filesystem, one CPU,
512 MiB memory without extra swap, 128 processes, and a 64 MiB restricted tmpfs.
These are conservative initial limits, not capacity guarantees. Measure the
isolated pilot and change the operator-owned limits through review if needed.
No runtime Docker administration or untrusted build execution belongs here.

## Private ingress and authentication

Networking intentionally matches the owner's current MCP: `network_mode: host`.
Hub listens on NAS port 8770; the existing MCP remains on 8765. Ensure 8770 is
free before installation. There are no Docker port mappings or Hub bridge
network to join. This is a LAN-reachable listener, not loopback-only isolation.
Existing firewall/Access policy is unchanged, and application auth still applies.

For a future operator-configured tunnel, a connector able to reach the NAS can
use `http://192.168.50.80:8770`. Only a connector using host networking on this
same NAS can use `http://127.0.0.1:8770`; a bridge/remote connector's own localhost
is wrong. Do not change any existing route as part of publication. Browser origin
is exactly `https://hub.ledgetoptechnologies.com`, without aliases.

Set `HUB_BASE_URL` to the exact HTTPS origin used by browsers and agents, without
a path or wildcard. The proxy must preserve the configured Host, forward SSE
without buffering, and maintain authenticated transport for `/mcp/` and the API.
The application uses that origin for OAuth redirects, cookie security, CSRF and
MCP DNS-rebinding checks. Do not disable those checks to repair a proxy mismatch.
If using `AGENT_BUS_ALLOWED_IPS`, verify the actual authenticated proxy/client address
seen by the application; do not assume it matches the bus's allowlist. Do not
trust arbitrary forwarded-IP headers or enable wildcard CORS.

The image disables Uvicorn access logs. Proxy/access/tracing logs must also omit
OAuth callback query strings, Authorization/Cookie headers, response cookies,
request bodies and CSRF values. OAuth codes must not reach operational logs.

In default GitHub mode, human OAuth remains unavailable until Beau configures a dedicated
GitHub OAuth application and the exact numeric GitHub allowlist ID using
`HUB_GITHUB_CLIENT_ID`, `HUB_GITHUB_CLIENT_SECRET`, and `HUB_BEAU_GITHUB_ID`.
Until then `/auth/me` reports anonymous and `/auth/login` fails closed. No first-
user enrollment, agent-token-as-human path, synthetic session, or dev-login
endpoint is available. The explicit browser demo is sample data only, never
authentication or permission to write live records.

### Approved alternate: Cloudflare Access human sign-in

Beau selected Access instead of requiring a new GitHub OAuth application for EH.
The GitHub implementation remains the compatibility default; the alternate is
selected explicitly, never inferred from a proxy header or a present email.
This is source/configuration support, **not a created Access application, DNS
record, tunnel route, changed Ops policy, or live authentication acceptance**.

Beau's latest choice is a single browser origin:
`https://hub.ledgetoptechnologies.com`. This supersedes the earlier two-hostname
plan. Do not configure the drone-services EH hostname or an alias redirect.

Supply the following through the protected Hub deployment environment:

```dotenv
HUB_HUMAN_AUTH_MODE=cloudflare_access
HUB_BASE_URL=https://hub.ledgetoptechnologies.com
HUB_ALTERNATE_ORIGINS=
HUB_CF_ACCESS_ISSUER=https://ledgetoptechnologies.cloudflareaccess.com
HUB_CF_ACCESS_AUDIENCE=<exact-EH-application-AUD-not-Ops-AUD>
HUB_CF_ACCESS_ALLOWED_EMAILS=bkoltz@ledgetoptechnologies.com,beaukoltz@ledgetopdroneservices.com
```

The team origin above was observed in Ops' unauthenticated HTTPS redirect; verify
it against the actual Zero Trust account before enrollment. Do not copy the Ops
AUD, cookies, session JWTs, or client secrets. No GitHub client secret is needed
in Cloudflare mode. Partial/missing configuration denies human access; invalid
complete configuration fails startup, and there is no fallback to GitHub.

Configure a **separate EH Access application** using the existing approved sign-in
provider and only the two exact email selectors above, not a domain-wide rule.
Keep existing Ops policies unchanged; reusing a broad mutable Ops policy would
couple its future membership changes to this administrative dashboard. Protect
the single EH hostname, with a valid certificate and reachable origin route.
Leave alternate origins empty; no second hostname, DNS record, tunnel route or
redirect is required for this deployment. The generic code's optional alias
support does not activate an alias by itself. Configure machine access separately
from the human-only browser Access policy.

The host-networking/tunnel instructions above apply. No tunnel, DNS record or
Access application was created or modified by image publication. Real ingress
and authentication remain separate owner-run setup and acceptance steps.

EH validates `Cf-Access-Jwt-Assertion` cryptographically with RS256, the exact team
issuer and EH audience, mandatory expiry/issued-at/subject/email/app-type claims,
and its own exact email allowlist. It never trusts the unsigned email header,
Ops tokens, service identities, or a browser's claim about its role. Keys are
fetched only from the configured team's HTTPS `/cdn-cgi/access/certs` endpoint,
with bounded response/time, on-demand cache refresh and failure cooldown, no
background poller, and no expired-key fallback. Requests lacking an assertion
cannot use an old local session to bypass Access.

After a verified sign-in, a separate hashed local session and CSRF value are
issued. Session identity includes issuer, EH audience, subject and email; expiry
does not outlive the initial Access assertion. Every human API request must still
carry a currently valid Access assertion. Human writes require the exact
canonical Origin and the local session's CSRF value. GitHub sessions do not work
in Access mode; changing providers requires sign-in again. Local logout deletes
the EH session only, **not the upstream Cloudflare Access session**. A later
explicit sign-in may therefore succeed without another identity-provider prompt.

The first **verified and explicitly allowlisted** sign-in pins that email's
Cloudflare subject for this issuer and EH audience in the same guarded local
transaction as session creation. This is not anonymous first-user enrollment.
The pin survives logout, session expiry, restart, and backup/restore. A later
matching-email/different-subject sign-in returns 403 rather than repinning; it
also preserves any existing valid local session. Legitimate identity-provider
account reprovisioning therefore needs an operator-reviewed re-enrollment plan.
There is no automatic email-account takeover, self-service pin reset, or reset
endpoint in this release. Include the identity table in coherent SQLite backups.

Both Hub and canonical-registry SSE routes emit an idle transport comment every
15 seconds, with authentication rechecked before each comment/event. Comments
retain the same pending source read/subscription, do not manufacture cursors or
readiness evidence, and do not poll task/message/readiness records or trigger
agent/model wakes or work items. Local authentication is rechecked on each send.
Revoked/expired streams terminate and pending reads are cancelled/awaited on
closure. Proxy buffering must remain off; real Cloudflare transport acceptance
is still required rather than inferred from synthetic ASGI tests.

Agent bearer authentication and the existing bus port 8765 remain independent.
Do not put a human-only browser challenge in front of the agents' established
route. Any separate tunneled machine route needs its own approved machine policy
and, if required, separate Cloudflare service credentials; an existing MCP bearer
alone does not satisfy Cloudflare Access. No machine route is enabled here.

Before activation, verify allowed/denied identities at the real Access edge and
EH, reject wrong-application/expired/forged assertions and direct-origin bypass,
exercise the chosen hostname, prove CSRF/logout/expiry, check all three agent routes,
and confirm existing Ops and message-bus clients still work. Source tests using
synthetic signing keys are not evidence of these live account/network checks.
Rollback selects the previous reviewed Hub image/configuration and removes only
the newly created EH routes/policies after preserving its state; do not alter Ops.

References: [Cloudflare JWT validation](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/),
[Access policy management](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/policy-management/).

Outbound network access is still needed for GitHub OAuth and other explicitly
configured adapters. This container is **not egress-isolated**, an untrusted-code
runner, or a security sandbox for arbitrary jobs. No canonical bus topic-registry
bridge, real host executor or Viewer pilot is activated by this deployment.

## Explicit activation and restart acceptance

Only after preconditions and release checks pass should the operator install
this standalone Compose using TrueNAS Custom Apps. Publication does not install
it. Use TrueNAS's supported update/redeploy path with image pulling enabled for
future versions; do not independently manage middleware-generated containers.

`restart: unless-stopped` applies only after the operator creates/starts this
service. It does not auto-create it, test boot readiness, or enable an executor.
Verify all of the following on the isolated pilot before declaring acceptance:

1. Control health succeeds on port 8770, while the existing bus on 8765 remains
   healthy and its client/lifecycle tests still pass.
2. Anonymous API/MCP requests and invalid agent tokens are denied. Human writes
   remain disabled without the dedicated OAuth configuration. Credentials never
   appear in logs or dashboard storage.
3. Native event support reports its actual state. Unsupported sources remain
   visibly unavailable; no polling or fake readiness is substituted.
4. After an intentional control-container restart, tasks and sessions retain
   their expected validity; state is not replaced or seeded with fixtures.
5. After a separately authorized host reboot, verify private ingress, control
   health and durable records again. A container-restart test is not reboot proof.
6. Topic readiness remains unknown without authoritative owning-session evidence.
   No host deployment, production change or real processing job is dispatched.

## Shutdown grace for an owned pilot

The standalone profile declares `stop_grace_period: 1200s` and the matching
`HUB_SUPERVISOR_STOP_GRACE_SECONDS=1200` operator assertion. This environment
value does not activate the pilot or select manifests. A future approved
bootstrap must explicitly pass the installed supervisor's configured grace as
`supervisor_stop_grace_seconds` to `create_owned_pilot_app`.

The owned factory rejects missing, noninteger or undersized grace before taking
ownership. The required budget is 1120 seconds: up to four original-command
inspections plus four phase submissions at the actual 125-second transport cap,
plus 120 seconds for cleanup. This is a conservative configuration contract,
not proof every dependency obeys its deadline or the supervisor installed it.
Autonomy status exposes required/configured grace and explicitly reports
`supervisor_configuration_verified=false` with `configuration_source` equal to
`operator_assertion`. Verify the deployed Docker/systemd/container-manager kill
deadline independently, including outer service wrappers. Never shorten it by
force-cancelling an accepted command and releasing its ownership lock early.

The control profile is source configuration only; it has not been applied to
TrueNAS. Actual installed grace, restart under load and real executor recovery
remain acceptance checks, not claims supported by a fixture's clean shutdown.

## Backup, isolated restore and rollback

Use the SQLite backup utility, not a raw copy of a busy database file: committed
WAL transactions must be included. Select a new backup path inside a restricted,
trusted local directory. The backup includes session/verifier data and must be
protected like the original state. POSIX files are created owner-only; Windows
inherits the parent ACL. The utility never collects environment/credential files.

```sh
python -m engineering_hub.backup /var/lib/hub/hub.sqlite3 /var/lib/hub/backups/new-reviewed-backup.sqlite3
```

The destination directory must already exist and the backup file must not exist.
This command is an example only; no live backup has been performed. A report
contains checksum, byte count, integrity status and permission metadata—not task
or session contents. Record the checksum and verify an isolated restored copy
with repository/auth tests before relying on it. Do not attach that restored
copy to production executors, notifications or the live ingress.

Keep the previous reviewed image digest and a verified compatible backup for
rollback. Stop and reconcile any uncertain action before rollback; do not infer
that a lease expiry killed a command. Restore only to the deliberately selected
Hub state directory under an approved maintenance operation. Never replace or
delete the message-bus volume as part of Hub rollback.
