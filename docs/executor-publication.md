# Approved staging executor publication route

Beau approved staging-only publication directly in this chat on October 26,
2026. The workflow permits manual dispatch from main with job-level
`packages: write`. This approval does not activate a staging runtime or change
production. Existing compatibility CI remains read-only. Publication success
must still be established by the actual run and verified registry receipt.

The already registered **Verify disposable executor compatibility** manual
workflow provides an opt-in route while GitHub has not indexed the new publisher
for direct dispatch. Its boolean `publish_staging` defaults to `false`: normal
compatibility runs keep their existing read-only permissions and steps. An
explicit `true` on main selects a separate job with the approved package-write
permission and calls the fixed local publisher workflow from the same commit.
The read-only job is skipped for that opt-in. The caller passes the same six
reviewed immutable inputs without changing them; supply the newer core tuple
listed below rather than compatibility's historical default core. The child
retains its manual/main guard and input/ancestry checks. No push trigger, generic
loader, credentials, normal control publication or `latest` promotion is added.

One manual main dispatch rebuilds the exact reviewed tuple
using the existing canonical wheel and compatibility helpers, then reinspect
the exact smoke-verified image ID, fixed defaults, source provenance and wheel.
Docker uses the existing Actions login. Only the publication step also receives
that same job token as `GH_TOKEN` for bounded, fixed GitHub package metadata
and pull-only registry reads with redirects disabled. The exact existing organization/package must be
private before upload; missing, public, wrong or unavailable metadata stops.
No additional secret, visibility, access, host enrollment or deployment change
is included.

The only destination is the existing private package
`ghcr.io/ledgetoptechnologies/engineering-hub-control`, with locator
`staging-executor-<run-id>-<attempt>`. Control `latest`, tested and release tags
are never changed. The locator is an intent tag, not an immutable reservation:
GHCR offers no atomic create-if-absent through Docker push. A unique attempt,
serialized workflow and exact absence preflight prevent ordinary duplicates;
another equally authorized racing writer could still move that tag. Consumers
must use the receipt's verified `@sha256:...` manifest digest as authority.

Absence is checked through fixed authenticated Registry V2 endpoints. The
existing job actor/token requests a fresh bearer token with only the exact
package's `pull` scope. First, that bearer must read the approved known core
manifest and match its raw SHA256 and config digest. Only then is the unique
intent tag read under the same bearer. Only HTTP 404 with the typed single
`MANIFEST_UNKNOWN` error permits a push. An existing manifest, authorization
failure, `NAME_UNKNOWN`, malformed response, redirect or unknown outcome stops.
Requests have a 20-second timeout, explicit byte bounds and no proxies; no token,
raw response, header or exception text enters receipts. Fixed phase and HTTP
status facts distinguish failed preflights without inferring their cause.
A local receipt is written with an unknown outcome before push. Runner loss
before artifact upload or interruption during a receipt overwrite can lose or
truncate it. An interrupted run remains unknown: independently reconcile the
original run/attempt tag before any further attempt; never blindly rerun. No
automatic retry occurs. A later attempt must use a new run/attempt locator.

Historical run `40380330557` stopped before any push at the former CLI absence
guard; its sanitized receipt could not establish the underlying CLI error.
Its private-package and installed-seam checks passed and its probe was removed.
That evidence remains valid; the revised protocol check does not relabel the
earlier failure or justify retrying the old intent blindly.

Registry raw manifest SHA256 and config digest must match the exact local
verified image. Both the tag and immutable digest are read back, then the
immutable reference is pulled and the installed import smoke runs with no
network, host mounts, operator configuration or launcher construction. Success
does not establish a staging pilot or authorize live execution.

The route pairs executor source `40d3d2f` and reviewed wheel `e9db6a97...`
with published core `66021ca0495c25d0e349b391c4cf624d4c6d7429`, manifest
`sha256:b75a9a69738926cc9a1533d46ff28728a76ab2cf1b5eb43746951e70bcd07839`
and config
`sha256:d8513fb7ad909b5a676a47e93854c5d1d8218e92ba7149409d388ed165456f60`.
The older core `40d3d2f` lacks required private-ingress verifier forwarding and
fails the mandatory pre-upload installed-seam probe. The probe still checks
actual installed build identity and explicit `access_verifier`
support on the frontend, ingress middleware, private gateway and owned pilot
function without constructing resources, launchers, issuers or configuration.
It runs by exact image ID, nonroot, read-only, with no network, host mounts or
capabilities and bounded CPU/memory/PIDs; image identity is checked before and
afterward. The unique inert container has an owner label and explicit create,
attach and terminal identity/resource inspection. A CLI timeout triggers cleanup
through the existing CI owner-check helper. Uncertain ownership refuses removal
and retains the sanitized unique locator with cleanup marked held; there is no
prune or blind retry. Before any campaign, independently verify this tuple's
installed verifier seam; publication and a real pilot remain unverified. Executor
source and core commits may intentionally differ; neither is inferred from a tag.

Only sanitized JSON receipts are uploaded: source/core/wheel identity, immutable
manifest/config digests, fixed stage/status, compressed layer total, config size
and Docker image size estimate. No wheel, image archive, raw manifest, private
files or credentials are exported. Private-package and installed-ingress facts
appear only after their respective checks pass. Compressed size and image size do not prove
staging import peak/quota headroom. Staging ingestion, space acceptance and
runtime workflow checks remain separate work, with no volume changes here.
