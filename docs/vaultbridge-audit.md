# VaultBridge audit

## Reviewed baseline

The system audited for this build is `mrtrollex/VaultBridge`, release v1.3.0, deployed at `http://192.168.50.80:30491`. The deployment is not changed by this repository. The plan's baseline records that its health endpoint is reachable on the deployed service and that API documentation is disabled in the deployed instance.

## Data model and API boundary

VaultBridge stores Markdown files as the source of truth and maintains a derived semantic SQLite index. Its HTTP API exposes create, append, read, list, search, related-note, duplicate-candidate, links, and backlinks operations. The source reviewed for the audit has no `delete_note`, `remove_note`, `move_note`, or equivalent service operation; delete, move, archive, and rename are therefore absent from the supported API surface.

That distinction matters: the Markdown vault is durable narrative state, the semantic SQLite database is an index, and the HTTP API is the controlled interface to those two layers. A successful HTTP response does not turn the system into a transactional transient queue. In particular, inbox messages would need explicit acknowledgement, leases, retry behavior, and cleanup, none of which are part of the audited VaultBridge contract.

## Suitability decision

VaultBridge remains useful for durable narrative notes and shared research. It is not a suitable transient Hermes/Codex inbox because the current API lacks message lifecycle transitions, atomic claims, retry leases, and retention semantics. The new bus keeps those concerns in a dedicated SQLite schema and leaves VaultBridge untouched.

## Safety boundary

This is a read-only record of the existing system. Do not delete, migrate, rename, archive, rewrite, or otherwise mutate current VaultBridge notes as part of this project. Do not mount the VaultBridge vault into the message-bus container. Cutover requires a separate approval after local, restart, security, backup/restore, and integration acceptance tests pass.
