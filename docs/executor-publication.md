# Approved staging executor publication route

Beau approved staging-only publication directly in this chat on October 26,
2026. The workflow permits manual dispatch from main with job-level
`packages: write`. This approval does not activate a staging runtime or change
production. Existing compatibility CI remains read-only. Publication success
must still be established by the actual run and verified registry receipt.

One manual main dispatch rebuilds the exact reviewed tuple
using the existing canonical wheel and compatibility helpers, then reinspect
the exact smoke-verified image ID, fixed defaults, source provenance and wheel.
Docker uses the existing Actions login. Only the publication step also receives
that same job token as `GH_TOKEN` for one bounded, fixed GitHub package metadata
read with redirects disabled. The exact existing organization/package must be
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

Only the CLI's exact manifest-not-found result permits a push. Existing tags,
denial, unavailable credentials, unknown CLI wording and network failures stop.
A local receipt is written with an unknown outcome before push. Runner loss
before artifact upload or interruption during a receipt overwrite can lose or
truncate it. An interrupted run remains unknown: independently reconcile the
original run/attempt tag before any further attempt; never blindly rerun. No
automatic retry occurs. A later attempt must use a new run/attempt locator.

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
