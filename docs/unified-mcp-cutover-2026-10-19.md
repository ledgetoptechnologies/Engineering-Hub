# Priority: Engineering Hub becomes the agents' sole MCP

Beau explicitly requested this cutover and retirement of the old service, while
keeping the full staging-management goal. This supersedes the earlier instruction
to leave both services permanently separate. It does **not** authorize deleting
message history, backups, agent credentials or unrelated services.

## Current implementation checkpoint

Hub integration source `925a31e07f46ecfd543bc895059b7e7788bf472a` reuses the
existing stateful bus FastMCP server rather than reimplementing its tools.
The combined default surface is 24 original tools plus nine Hub tools. Existing
tool names, session-owner checks, recipient resources, push publisher and the
bus lifespan are retained. Explicit original-path ASGI routes preserve legacy
REST endpoints; only the agent listener exposes them. Browser ingress denies
MCP and bus REST paths before human authentication.

`--unified-mcp --split-ingress` explicitly selects the new composition. Plain
Hub startup is unchanged. The factory requires existing absolute DB/audit/object
paths, a separate Hub DB, shared agent authentication, a preconfigured exact
transport policy and native push. It must suppress startup retention until the
preserved-state comparison is complete. Factory-only imports must be inert.
Unified health retains the old top-level push/cursor fields and also reports its
bus component. Readiness requires valid audit, healthy DB and native push with
subscriptions enabled; HTTP 200 alone is not readiness.

Claude owns the bus factory hooks and their tests. Codex owns Hub composition,
ingress and integration tests. Hermes owns host/bootstrap cleanup and the actual
deployment inventory/backup/single-writer handoff. No overlapping host mutations.
These assignments were sent through the existing linked MCP discussion; pending
messages do not prove receipt or an active execution process.

## Already executed, limited evidence

The Hub integration snapshot passed **247 existing Linux regression tests** in
17.12 seconds, ResourceWarning and unraisable warnings treated as errors, with
one known Starlette deprecation warning. The network-none, read-only,
capability-dropped test container had no host/socket mounts or published ports
and was removed. This proves existing Hub regressions only, **not** the new
unified composition: its 26 new tests still need the bus hooks integrated.

An independent read-only review identified and corrected the stricter unified
health and pre-construction Cloudflare configuration gates. Source review is
not deployed acceptance. The existing tested application artifact predates the
unified changes and must not be deployed as the unified MCP.

## Deployment sequence; no old-data deletion

1. Inventory the exact old bus and current Hub images, mounts, ownership, DB,
   audit, object/history roots, semantic caches, runtime settings and all agent
   clients. Identify any installed topic/registry/session routes absent from this
   checkout. Preserve them or block cutover; tool parity alone is insufficient.
2. Integrate and independently review Claude's bus hooks. Execute combined
   discovery, auth/session isolation, claim/ack/reject/deduplicated-send,
   knowledge CAS, file history, feedback, cursor and native notification tests.
   Build and pin the actual unified image; semantic model stays available offline.
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

Expected new public agent endpoint remains the Hub agent listener at
`http://192.168.50.80:8770/mcp`; browser stays loopback 8771 behind its existing
tunnel and separate Access policy. Final ports/mounts must match the actual
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

No current client configuration, production image, active endpoint or old service
has changed at this checkpoint. The dedicated-MCP cutover and the full autonomous
staging/real rollout/rollback/UI/reboot goal remain incomplete.
