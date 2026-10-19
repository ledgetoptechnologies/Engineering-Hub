# Priority: Engineering Hub becomes the agents' sole MCP

Beau explicitly requested this cutover and retirement of the old service, while
keeping the full staging-management goal. This supersedes the earlier instruction
to leave both services permanently separate. It does **not** authorize deleting
message history, backups, agent credentials or unrelated services.

## Current implementation checkpoint

### Published alias-capable candidate and installed-client evidence

The alias-capable main release `abd956740c2dda65911cc0601ee86210a8a2a6c6`
is published and independently checked at
`ghcr.io/ledgetoptechnologies/engineering-hub-control@sha256:5e64a12a3355fa073b73fbe737a90ba131f1a1c21e18fdfee83064057b5d6042`.
Workflow `39340339288` passed candidate and authenticated registry-image checks;
the original registry manifest and promoted `latest` manifest hashes match.
The older release discussed below is historical, not the installation candidate.
Do not substitute an unverified moving `latest` tag for the recorded digest.

The subsequent main `74782aa08a38f811f4f11b26b42c49b83f11179f` release also
passed workflow `39341366339` and was verified at immutable registry digest
`sha256:c812b07cd48a955f69e694fde95bcdd433a9a4716168c7a57ffcd8fb9af4e3c0`.
Downloaded original/promoted manifest hashes match that digest; actual pulled
image tests again pass both ports/shared session/native alias notification and
synthetic nine-table preservation across recreation. This includes the source
bootstrap-unit whitelist update, not installation of that unit on staging.
Neither published release establishes actual NAS data or client acceptance.

Claude's read-only installed-client report at 2026-10-19 23:20 UTC identifies
the old endpoint as bus version 0.6.0/schema 4 with 24 tools, recipient inbox
resource/template, `/api/messages/wait`, and no deployed topic-registry routes
in that service. His routing registry is client-local, supplemented by knowledge
documents; transport presence is not session readiness. This is a collaborator's
runtime report, not a Codex inspection of the installed image or database.
Preserve those local registries/cursors and knowledge records; disabling the new
optional registry bridge is not a loss of a verified existing server capability.
Other agents must still confirm their own actual dependencies.

### Operator acceptance gates before updating the existing app

1. Record the actual two container IDs, image digests, mount sources, effective
   numeric UID/GID, schema and semantic-index/cache locations. Inspect only
   selected metadata, never full environment/credentials. Record old ownership
   and permissions of both DBs and their WAL/SHM siblings, audit and objects.
   UID/GID 568 must have the required access. A mismatch is a stop condition,
   not permission to recursively chown existing data or weaken ACLs.
2. First test coherent copies with the exact candidate. For the live window,
   stop **both** old bus and existing Hub writers, verify both terminal, then
   snapshot/backup both state roots including their dependent datasets. Record
   actual snapshot identifiers and a tested restore procedure. Never snapshot
   an assumed dataset name or treat a live SQLite file copy as coherent.
3. Check measured memory for the current services and model workload. The
   reviewed Compose retains 512 MiB; previous bounded synthetic tests do not
   establish sufficient headroom for the live corpus/concurrency. Do not
   silently raise limits to an arbitrary value or proceed after an OOM.
4. Coordinate the split image and tunnel upstream in the same window: Codex's
   read-only Cloudflare dashboard inspection on 2026-10-19 showed the Hub route
   in the Ledge Top Technologies tunnel using `http://192.168.50.80:8770`.
   Recheck that route at installation rather than assuming it cannot change.
   After confirming
   the connector's network namespace can reach host loopback, use browser-only
   `http://127.0.0.1:8771`. If it cannot, stop and resolve the private connector
   path; do not bind browser ingress publicly as a workaround. Confirm browser
   login and both allowed identities, denied identity/LAN human access, and
   `/mcp`/bus REST denial through the public hostname even with an agent bearer.
5. Reserve a five-minute initial health-verification window after startup. Any
   failed DB/audit/push component, OOM/restart, cursor regression, parity or
   identity failure stops acceptance. Do not wait indefinitely or call HTTP 200
   healthy. Preserve diagnostics without secrets and quiesce the new owner
   before recovery; do not start the old owner alongside it.
6. Recovery is **not** blindly rolling datasets back: retain a coherent copy of
   all post-cutover writes before any restore. Even unchanged schema 4 alone
   does not prove downgrade compatibility. Verify old-image compatibility on
   copies, reconcile new writes, and choose an operator-reviewed recovery plan
   before making an older snapshot authoritative. Preserve both old app
   definitions/images and backups until all three installed clients, native
   wake, human UI and restart acceptance pass.

These gates are not evidence of execution. The published container fixtures
still use synthetic candidate-bus data, not the deployed older image/state.

### In-place upgrade and port compatibility

Beau confirmed the existing Hub is already running. This is an **in-place
upgrade**, not a new Hub installation or an empty replacement database.
`compose.hub-unified.yaml` is a candidate profile for that existing app. Its Hub
bind follows Beau's subsequently supplied existing Hub Compose, superseding
Hermes's older prepared artifact:
`/mnt/Plugins/App_Data/Engineering-Hub/` to `/var/lib/hub`.
Preserve the existing Compose project identity `e-h-control` and service
`engineering-hub-control`. Do not append an `engineering-hub` child directory:
that would select a different database rather than upgrade the existing Hub.
Its separate legacy bus bind follows Beau's supplied configuration:
`/mnt/Plugins/App_Data/Agent_MCP` to `/var/lib/agent-message-bus`.
Neither path is automatically created. Reconcile both against actual installed
mounts before using the profile; a configuration file is not a container inspect.

The optional `--agent-alias-port 8765` keeps **both** existing agent addresses:
`http://192.168.50.80:8765/mcp` and `http://192.168.50.80:8770/mcp`.
Both sockets belong to the **same Uvicorn agent server, Hub core, stateful MCP
session manager and lifecycle**. No second database writer or MCP stack is
launched. The browser remains `127.0.0.1:8771`, behind the existing Hub tunnel.
Defaults are unchanged unless the alias option is explicitly selected.
Invalid or occupied alias ports fail before core construction and database work.

Actual local image source `9193ff7889ee732ea4c8821ace7b79cb89164a9d` passed the
updated synthetic 24-to-33-to-33 preservation smoke with both ports: a primary
session ID reused on the alias returned the same 33 tools; a foreign identity
was rejected on both; alias-originated sends notified a primary-port subscriber.
Its image index is
`sha256:2d20bee4535fef1391c8bdcdb918df5b61f0bc19beb4456881dac58d39276bcb`.
This is local container evidence, not an installed NAS or agent-wake claim.

The earlier unified release from main
`132236f1da272849548b0b0b4463a5d2d8934aa2` was privately published by workflow
`39335110935` at registry digest
`sha256:3ab836cac7c7834981f5b9b3a568fe6edf147b64fd49d188ecf37743c0f74812`.
Downloaded publication evidence confirms authenticated registry startup,
split ingress and synthetic unified preservation; registry and promoted-latest
manifest hashes match. **That release does not include the alias option** and
must not be used with this new three-port profile. Wait for the matching
alias-capable release and its verification before the actual cutover.

### Retention and authentication gates

`HUB_BUS_STARTUP_CLEANUP=0` is the default and explicit preservation-phase
setting. It prevents startup cleanup from removing retention-eligible records or
expiring claims before source/copy comparisons. **Cleanup is suspended in this
mode**; merely keeping retention-day environment values does not run it.
After copied-state parity and all three client canaries are verified, set it to
`1` to restore the legacy cleanup-at-startup policy on subsequent starts and
verify the expected retention transitions. Only exact `0` and `1` are accepted.
This does not introduce a periodic maintenance worker or permission to replay
work after a claim expires. The existing admin cleanup endpoint is retained.

Live read-only checks still found the existing Hub serving HTML on LAN port
8770 and returning 401 for an anonymous protected overview. Therefore its
working dashboard is not yet proof of the final tunnel-only browser layout.
Beau's supplied existing Hub Compose has GitHub mode with no GitHub credentials
and empty Cloudflare fields. This explains why passing Cloudflare's outer login
does not establish a Hub session; the running old build still reports unauthenticated.
The candidate profile uses the separately approved Cloudflare Access configuration,
not an additional GitHub OAuth application. This owner-supplied Compose is stronger
configuration evidence than the older prepared artifact, but is not an independent
container mount/image inspection or end-to-end browser authentication test.
Reconcile the existing approved Hub Access issuer/audience/email policy and
tunnel upstream before switching to split ingress. Preserve the Hub URL,
existing data and separate Ops policy; do not overwrite credentials from chat.

Hub integration source, including Claude's bus hooks at
`fc9efac68a351337b393eda7330ebb414e6d3b85` and the explicit startup-cleanup hook
at `2b1607b686fe7021e0998df7a67c96d52f61e158`, reuses the
existing stateful bus FastMCP server rather than reimplementing its tools.
The combined default surface is 24 original tools plus nine Hub tools. Existing
tool names, session-owner checks, recipient resources, push publisher and the
bus lifespan are retained. Explicit original-path ASGI routes preserve legacy
REST endpoints; only the agent listener exposes them. Browser ingress denies
MCP and bus REST paths before human authentication.

`--unified-mcp --split-ingress` explicitly selects the new composition. Plain
Hub startup is unchanged. The factory requires existing absolute DB/audit/object
paths, a separate Hub DB, shared agent authentication, a preconfigured exact
transport policy and native push. The unified factory explicitly passes
`startup_cleanup=False` in preservation mode: construction must not run legacy
retention before the preserved-state comparison. The explicit post-verification
mode above restores cleanup. The standalone legacy default remains unchanged.
Factory-only imports are inert; the default ASGI entry is lazily constructed.
Unified health retains the old top-level push/cursor fields and also reports its
bus component. Readiness requires valid audit, healthy DB and native push with
subscriptions enabled; HTTP 200 alone is not readiness.

Claude supplied the integrated bus factory hooks and their tests. Codex owns Hub composition,
ingress and integration tests. Hermes owns host/bootstrap cleanup and the actual
deployment inventory/backup/single-writer handoff. No overlapping host mutations.
These assignments were sent through the existing linked MCP discussion; pending
messages do not prove receipt or an active execution process.

## Executed local evidence; not an installed cutover

An actual `docker/Dockerfile.hub-control` image was built from the exact
`2b1607b686fe7021e0998df7a67c96d52f61e158` source archive, using local tag
`ghcr.io/ledgetoptechnologies/engineering-hub-control:build-local-unified-2b1607b`.
It is **local only, not published or deployed**. Recorded OCI identities are:

- Index: `sha256:7ad8780fd4789e05c2e58aac14ee17d29fa9bd0ad1f3d42d6a880d1308c46b59`
- Platform manifest: `sha256:7c74062c98ba5a7937f1eec5df493f9a45993728614022b31cc1ed26fc72aae7`
- Image config: `sha256:74d22ecb1590517b6b85975e9c5152e56b0fa48253b7748ba8dce32b24350258`

An optional local `docker image save` artifact is
`C:/Users/fstor/Documents/Codex/eh-control-unified-2b1607b-7ad878.tar`
(178,923,520 bytes, SHA-256
`91b4b72f310f22e55e3f8487f789ca86128f845ad63d9e9d9800d178cfaed96b`).
The existing bounded archive-inspection helper resolved its OCI root/platform/
config chain to the identities above without extracting it. This is transport
preparation, not a registry upload, remote import or deployment.

The actual disposable cutover smoke passed three sequential processes over one
owned named volume: **legacy-only 24 tools → unified 33 tools → recreated unified
33 tools**. Before any new canary operation on each unified startup, it compared
row counts and content hashes for all **nine legacy tables**, stored file bytes,
audit byte count and audit content hash with the previous process's saved state.
Pending/claimed messages, sender-deduplicated sends and receipts, knowledge CAS,
versioned file history, feedback and cursor continuity were exercised. Actual
native resource notifications and cross-agent/session isolation passed. No old
message was imported as a new event.

That fixture used **synthetic legacy data and the candidate image's own bus
implementation**, not the older deployed bus image or a production-state copy.
It ran network-none, read-only, UID/GID 568, all capabilities dropped, no new
privileges, 512 MiB memory with no extra swap, one CPU and 128 PIDs. It exposed no
host port, host path or Docker socket. Graceful owned shutdown and disposable
volume cleanup are fixture checks, not reboot recovery.

The same actual image also passed the default-release and split-ingress smokes,
and task/decision persistence across recreation. A separate offline semantic
embedding check passed as UID 568, network-none and local-files-only, producing
one 384-dimensional embedding. ONNX emitted a read-only telemetry warning; this
is not proof of production semantic-cache inventory or a full-corpus search.

Linux focused unified/factory validation passed **40 tests in 6.11 seconds** on
the `2b1607b` overlay, explicitly selecting its checkout with `PYTHONPATH`. An
earlier focused run passed 80 tests. The preceding full integration snapshot
`738e4beaef0558c28f0d5ed1ef16cbbdc0740bba` passed **3,707 tests, six skipped and
three warnings in 204.32 seconds**; that full run predates the startup-cleanup
addon. The source-selection correction is
`2798b566cf4c5503e1e7ba59c74ee13630ade4b8`. Its exact Linux source-overlay full
validation completed: **3,722 passed, six skipped and three warnings in 194.34
seconds**, with no exclusions or subprocess source-selection workaround. The
warnings are the known Starlette deprecation and two multithreaded-fork
deprecations. This is source integration evidence, not installed-host acceptance.

Publication workflow source at
`bc5c6587e1c9c2377598fa9b93f70c69071744eb` gates upload on the local unified
preservation smoke, then repeats it after pulling the published registry digest
before promotion. This is checked-in gating, **not evidence that registry
publication or promotion has occurred**. Independent review drove the stricter
health, pre-construction authentication and non-forceful fixture-cleanup checks.

## Deployment sequence; no old-data deletion

1. Inventory the exact old bus and current Hub images, mounts, ownership, DB,
   audit, object/history roots, semantic caches, runtime settings and all agent
   clients. Identify any installed topic/registry/session routes absent from this
   checkout. Preserve them or block cutover; tool parity alone is insufficient.
2. Complete final-source regression/review and publish/verify the immutable
   unified image through the gated workflow. Local composition, synthetic state
   preservation and offline model execution now have evidence above; they do not
   replace checking the actually installed old version and its capabilities.
3. Validate that exact image against a coherent **copy** of state first. Never
   construct against the live old paths while the old service can write: bus
   construction opens SQLite and audit locking is process-local. Keep the two
   databases separate; do not import old mail as new events or replay it.
4. Establish a brief coordinated maintenance window. Quiesce the exact old bus
   and Hub writers through the existing operator channel, verify them terminal,
   and take a coherent SQLite/WAL/objects/audit backup or filesystem snapshot.
   Record table contents/counts, delivery sequence/head, receipts/tombstones,
   pending/claimed states and expiries, knowledge versions, immutable file/history
   bytes, feedback revisions and audit-chain prefix. Retain rollback images and
   stopped definitions. No blanket chown or unrelated prune.
5. Start only the new unified owner with the verified original bus paths and
   separate existing Hub state. Preserve all three tokens and source-IP rules.
   Compare state **before canary operations**; receive/claim and audit operations
   may legitimately mutate state afterward. Preserve cursors and offline mail.
6. Canary each agent on the selected new endpoint, keeping server names and
   credentials. Verify authenticated discovery, correct legacy reads, cross-agent
   denial, cursor continuity, a marked idempotent send/claim/ack exchange and a
   real native notification into the intended session without polling fallback.
   All agents must confirm their own client/listener configuration, not just a
   server health probe. Codex currently has an explicit six-tool allowlist; expand
   it to the verified combined surface only at this stage.
7. After all three canaries and data/push checks pass, disable/remove the old
   **running service** and obsolete client registrations. Preserve its data,
   coherent backup, immutable rollback image and configuration. Report exact
   endpoints and evidence. Do not say removed while an old writer remains live.

The unified owner retains the Hub agent listener at
`http://192.168.50.80:8770/mcp` and the compatibility address
`http://192.168.50.80:8765/mcp`; browser stays loopback 8771 behind its existing
tunnel and separate Access policy. Final mounts must match the actual
operator inventory before any Compose update. No tunnel, Ops or email-policy
change is needed merely to unify MCP tools.

## Recovery

If a canary fails, stop new writes and preserve their records before changing
ownership again. Do not blindly point back to an older snapshot or run both
writers. A rollback must account for every accepted post-cutover write, retain
its sender-scoped request identity and resume only one coherent owner. Temporary
read-only service is an option only if an actual reviewed implementation exists;
the current old service has no assumed maintenance-mode switch.

## Not done yet

Hermes does not currently have an authenticated TrueNAS management channel for
this operation. The exact installed images, mount layout, full client inventory,
coherent cold backup and single-writer handoff remain an operator gate. No
TrueNAS deployment, current client/listener configuration, active endpoint or old
service was changed by the local checks. No tunnel, Ops, credential or Access
policy change was made. Do not report the old MCP retired yet.

Still unproved: upgrading the actual deployed old image/state, all three agents'
installed client canaries and correct-session wake, real NAS/app acceptance,
reboot recovery and the full autonomous staging rollout/health/rollback pilot.
The dedicated-MCP priority and the entire original staging-management goal remain
incomplete; successful container fixtures do not narrow that goal.
