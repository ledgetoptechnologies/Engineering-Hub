# Pilot loopback executor proxy renderer

`scripts.render_loopback_executor_proxy_bundle.render(...)` is a pure operator
renderer. It returns proposed NGINX configuration, a manual systemd unit and
private NSS files in memory. It creates no files, processes, credentials,
enrollment or settings and does not activate or integrate a runtime.

The pilot coordinator cannot traverse the rootless executor's private API parent
under the previously measured host UID mapping. This separate proxy proposes a
root NGINX master with a fixed UID 210000 worker. Only the exact API parent, pinned
NGINX binary alias and existing TLS material enter its sandbox. Engine sockets,
bearer credentials, signing keys and databases are excluded. The executor still
authenticates callers; the coordinator still verifies signed observations.

The renderer reuses the unchanged production proxy HTTP template: four exact
routes, original-URI/method checks, bearer preservation, 8 KiB body limit and no
cache or upstream retry. It narrows listener and peer ACL to literal 127.0.0.1.
The existing NAS renderer and policy remain unchanged.

Required operator inputs are a direct campaign child beneath
`/var/lib/eh-pilot-volume-test-v1`, a canonical v4 generation UUID, exactly
`<campaign>/hub-pilot-executor-<generation>/executor-state/api`, existing distinct
certificate/key paths beneath that campaign and worker UID 210000. The HTTPS
origin must be literal 127.0.0.1 on an explicit non-live port; the operator must
also ensure it differs from the supervisor's other ports. TLS must include a
literal 127.0.0.1 IP SAN and match the protected profile's CA pins.

Rendering checks lexical bindings, not live ownership, freshness or readiness.
Before installation or activation, independently verify current boot, actual
socket/path ownership and inodes, UID/GID mapping, installed launch/config/release,
NGINX binaries/libraries, TLS validity and spare capacity. The proposed unit is
manual-only, with no restart or boot installation section, capped at 64 MiB,
100 mCPU and 16 PIDs. A changed UID map requires a newly reviewed binding rather
than weaker UDS permissions. Drain accepted coordinator work before the owned
proxy master's QUIT, then drain the separately owned executor through its
existing launcher protocol. No automatic cleanup, adoption or force-kill is
introduced. This does not establish a live Hub/NAS/phone route or pilot acceptance.

The focused tests run pure contracts on Windows and actual local NGINX
TLS-to-UDS wire checks on Linux when the trusted binary is supplied. Wire tests
cover exact routes/authentication, peer/URI/framing/body/TLS rejection, no retry,
nonce/signed-byte preservation through the production HTTPS client and graceful
QUIT across repeated Python owner cancellation. They use temporary path aliases,
same-UID UDS application, synthetic command/issuer adapters and ephemeral test
TLS keys. They do not prove the installed root systemd sandbox, UID 210000 mapping,
real executor/issuer, TEST Engine, staging connectivity or host resource limits.
