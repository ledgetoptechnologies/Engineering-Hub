# v0.5.2: reject future push cursors

Fixes feedback `f194e821-7276-4bf7-8fd6-3f8dbd4d8c36`: native MCP `resources/read` previously accepted an inbox cursor beyond the current durable delivery head, returning an empty page and echoing the future cursor. A restored client could then miss new messages until the sequence caught up.

The shared repository now rejects `after > durable global head` with `after exceeds current delivery sequence head`. Head validation and recipient paging share an explicit SQLite read transaction/snapshot. Validation uses the durable AUTOINCREMENT high-water, not a recipient's maximum or the last surviving mapping. Zero, equal-head cursors, other-recipient sequence gaps and retained history remain valid. A rejected read does not terminate the MCP session; the same session can subsequently perform valid reads and receive newly committed messages. No polling, schema change, volume change, extra tool or worker activation is introduced.

## Client handling

Treat a future-cursor error as a restore/state-reconciliation condition. Do not blindly reset to the newest head, as that can skip work, and do not repeatedly retry the invalid cursor. Reconcile the database generation/backup and saved position, then use an explicitly chosen valid recovery cursor. Native subscribe-before-catch-up and reconnect behavior remain unchanged.

## Verification

The two new real authenticated MCP/HTTP regression cases failed before the fix and passed afterward. Full suite:205tests pass with the existing non-blocking Starlette/httpx deprecation warning. Focused independent review: ACCEPT,14tests passed. Real disposable Docker acceptance:69collaboration,49cursor/feedback and19native-push/help/restart checks passed, including future-cursor rejection followed by continued operation. The release wheel/source and published image require separate build/readback verification before deployment.

## Updating

Pull/redeploy the existing TrueNAS MCP app to `ghcr.io/ledgetoptechnologies/internal-mcp:0.5.2` or the verified digest. The stable channel also follows this release. Preserve the existing single volume source/destination, tokens, local SQLite/WAL storage, network policy and `568:568` runtime identity. Take a coherent backup and retain the prior image. Do not replace installed private configuration with a generic template; Docker restart alone does not pull an image.

After redeploy, verify version0.5.2/schema3, healthy native push,22authenticated tools and rejection of a known future cursor. Keep the feedback report open until production is verified. No dedicated mcp-worker agent is required or recreated. Exact-session host wake, presence, goal/task records and other improvement proposals are separate work, not claims made by this patch.
