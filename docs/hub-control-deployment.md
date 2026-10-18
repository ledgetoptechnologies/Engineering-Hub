# Private Hub control deployment — operator-gated

This standalone Compose profile is deployment configuration, **not evidence of a
running service or release acceptance**. It does not activate anything by being
committed. The working message bus, its images, volumes, port 8765, MCP endpoint
and clients remain unchanged. Do not combine this file with either bus Compose
file. The control image is built from `docker/Dockerfile.hub-control` and serves
its compiled dashboard plus the independent Hub API/MCP on container port 8770.

## Preconditions and private storage

- Choose a dedicated, protected **local ZFS** dataset directory for Hub state.
  Set `HUB_STATE_DIRECTORY` to its absolute host path. Verify the actual mount
  backing it: it must not be SMB, NFS, the bus state directory, a checkout, or a
  processing data share. Production and staging must use different directories.
- Create the directory outside Compose with ownership UID/GID `10001:10001` and
  owner-only directory permissions, or equivalently restrictive TrueNAS ACLs.
  Preserve required administrator recovery access. Verify inherited ACLs; do not
  recursively change ownership of a pool, existing share, or unrelated data.
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

Set `HUB_CONTROL_IMAGE` to an independently reviewed, tested control-image
reference ending in `@sha256:` plus exactly 64 lowercase hexadecimal characters.
Record that digest and source revision in release evidence. **Compose's required
string interpolation does not enforce immutable image syntax.** Before pulling
or starting, reject tag-only references and validate the image against the
reviewed build/provenance record. This validation example prints no environment
values and performs no deployment:

```sh
python -c "import os,re; assert re.fullmatch(r'[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64}', os.environ.get('HUB_CONTROL_IMAGE','')), 'An immutable reviewed image digest is required'"
```

The profile limits the service to a read-only non-root filesystem, one CPU,
512 MiB memory without extra swap, 128 processes, and a 64 MiB restricted tmpfs.
These are conservative initial limits, not capacity guarantees. Measure the
isolated pilot and change the operator-owned limits through review if needed.
No runtime Docker administration or untrusted build execution belongs here.

## Private ingress and authentication

The only host publication is `127.0.0.1:8770`. There is no direct LAN or public
listener. Use an independently reviewed private HTTPS reverse proxy on the same
host, with LAN/VPN access restrictions. A containerized proxy cannot reach host
loopback using its own loopback: review a host-reachable arrangement before
changing networking. Do not casually publish Hub on all interfaces.

Set `HUB_BASE_URL` to the exact HTTPS origin used by browsers and agents, without
a path or wildcard. The proxy must preserve the configured Host, forward SSE
without buffering, and maintain authenticated transport for `/mcp/` and the API.
The application uses that origin for OAuth redirects, cookie security, CSRF and
MCP DNS-rebinding checks. Do not disable those checks to repair a proxy mismatch.
If using `HUB_ALLOWED_IPS`, verify the actual authenticated proxy/client address
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

The agreed browser addresses are:

- Canonical: `https://eh.ledgetopdroneservices.com`
- Navigation alias: `https://eh.ledgetoptechnologies.com`

Supply the following through the protected Hub deployment environment:

```dotenv
HUB_HUMAN_AUTH_MODE=cloudflare_access
HUB_BASE_URL=https://eh.ledgetopdroneservices.com
HUB_ALTERNATE_ORIGINS=https://eh.ledgetoptechnologies.com
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
both hostnames, with a valid certificate and reachable origin route for each.
For the alias, prefer an edge navigation redirect to the canonical origin. Hub
also supports this exact configured alias as defense in depth: GET/HEAD browser
navigation redirects to the canonical root without forwarding query strings;
API/MCP/other writes on the alias fail with 421 rather than forwarding credentials.
Agents must use the canonical machine route, not the browser alias.

The loopback port binding remains unchanged. A `cloudflared` container cannot
reach `127.0.0.1:8770` in the host namespace through its own loopback. Review the
actual existing connector/proxy topology before selecting its private origin;
do not expose 8770 to the whole LAN or mount a Docker socket as a shortcut.

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

Agent bearer authentication and the existing bus port 8765 remain independent.
Do not put a human-only browser challenge in front of the agents' established
route. Any separate tunneled machine route needs its own approved machine policy
and, if required, separate Cloudflare service credentials; an existing MCP bearer
alone does not satisfy Cloudflare Access. No machine route is enabled here.

Before activation, verify allowed/denied identities at the real Access edge and
EH, reject wrong-application/expired/forged assertions and direct-origin bypass,
exercise both hostnames, prove CSRF/logout/expiry, check all three agent routes,
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

Only after the preconditions and reviewed release gates pass should the operator
explicitly select this standalone file and `hub-control` profile. An example
activation command is shown for the operator; it has not been run:

```sh
docker compose -f compose.hub-control.yaml --profile hub-control up -d
```

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
