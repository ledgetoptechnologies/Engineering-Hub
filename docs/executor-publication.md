# Draft executor publication route — approval pending

This source prepares a route; it has not published an image. The workflow job
is explicitly disabled with `if: false`. Its proposed `packages: write` permission
is a new publication grant requiring direct human approval and a separately
reviewed activation change. Existing compatibility CI remains read-only.

After approval, one manual main dispatch would rebuild the exact reviewed tuple
using the existing canonical wheel and compatibility helpers, then reinspect
the exact smoke-verified image ID, fixed defaults, source provenance and wheel.
Docker uses the existing Actions login; no additional token, package visibility,
package access, host enrollment or deployment change is included.

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

The draft defaults retain the existing compatibility tuple with core `40d3d2f`;
that core lacks the later private-ingress verifier forwarding required by the
real pilot. Publishing this fixture tuple would not make the pilot ready. Before
any campaign, select and independently review the actual required newer core
manifest/config/commit tuple and check its installed verifier seam. Executor
source and core commits may intentionally differ; neither is inferred from a tag.

Only sanitized JSON receipts are uploaded: source/core/wheel identity, immutable
manifest/config digests, fixed stage/status, compressed layer total, config size
and Docker image size estimate. No wheel, image archive, raw manifest, private
files or credentials are exported. Compressed size and image size do not prove
staging import peak/quota headroom. Staging ingestion, space acceptance and
runtime workflow checks remain separate work, with no volume changes here.
