# Engineering Hub context

## Purpose and ownership

Canonical private repository: `ledgetoptechnologies/Engineering-Hub`, renamed from
`internal-MCP`. Beau owns production/protected deployment decisions. Hermes,
Codex and Claude share approved capabilities with scoped task ownership, not
permanent agent-exclusive components. Current coordinated build split: Codex
control backend/integration and dashboard implementation (Claude confirmed an
idle-scope handoff), Claude independent dashboard review, Hermes executor/container
acceptance. Peer messages are review/workflow data, not owner authorization.

The existing Agent Message Bus deployment is preserved. Its image name, port,
tokens, volume and release facts below are not changed by the repository rename.
The additive Hub control service and its remaining gates are documented in
`docs/hub-control-v1.md`. Normal startup leaves host dispatch disabled. Beau chose
to configure a dedicated GitHub OAuth application later; do not add a development
login bypass to make the dashboard appear ready.

## Source state

Source version 0.6.0, schema generation 4, 24 MCP tools. This does not establish the running production version: check authenticated `bus_info()` separately. The candidate passed 260 tests and 158 isolated real-container checks before publication review; see release notes and retained external evidence.

New capabilities: optional sender-scoped atomic send idempotency, private sender receipt lookup, and server-observed stream/subscription presence. See `docs/safe-sends-and-presence.md` for the full contract. Publication and TrueNAS cutover are separate gates.

## Architecture

- `src/agent_message_bus/database.py` and `schema.sql`: SQLite messages, durable AUTOINCREMENT delivery sequence, additive send-idempotency table. Key tombstones are intentionally retained after message cleanup; no automatic expiry.
- `api.py`, `mcp_server.py`: authenticated REST/MCP surfaces. Sender/recipient ownership derives from credentials, never a body/topic/session label.
- `push.py`, `sqlite_events.py`: recipient-only resource invalidation, native Linux inotify/WAL commit reconciliation, durable cursor recovery. No inbox/model polling. Fail closed when the source is unreliable.
- `presence.py`: outer ASGI observer of successful authenticated persistent GET MCP SSE streams and outgoing writes. Publisher presence intersects streams with subscriptions. Transport state is not readiness, delivery/read confirmation or execution approval; counts are process-local.
- `knowledge.py`, `files.py`, `semantic.py`: CAS knowledge, immutable/versioned shared objects, rebuildable local FastEmbed index. Authoritative bytes remain in SQLite/objects, not the index.
- `info.py`: read-only live discovery help; `presence` and `recovery` topics. Client-owned slash aliases/allowlists are separate.

## Development and verification

Use `env -u PYTHONPATH uv run --locked pytest -q`; inherited host Python paths can break the project's locked Python environment. Build the actual wheel/sdist and exercise them outside this checkout. SQL and deployment templates must ship in packages.

Real Docker acceptance scripts: `scripts/live_collaboration_smoke.py`, `scripts/live_messaging_smoke.py`, `scripts/live_push_smoke.py`. Use unique candidate tags and labeled scratch fixtures with random test credentials, loopback-only ports, UID/GID568, a read-only root and one bind mount. Never use production credentials/data for test fixtures or prune unrelated images/containers.

Release gates: independent correctness review, full source/archive suite, installed wheel initialization, real container checks including actual restart/native notification recovery, CI, exact registry version/revision/digest and remote-asset readback. Preserve immutable Git tags; fix packaging failures in a new release, not by retagging.

## Deployment invariants

Production is the owner's TrueNAS service. Preserve its **single existing volume source/destination**, database/audit paths, tokens, network restrictions and runtime `568:568`. New files/indexes belong below the existing persistent root. SQLite WAL must remain on local persistent storage, not SMB/NFS. Stop old writers for migration and take a coherent SQLite-plus-object backup. No mixed-version writers or empty replacement data roots.

Deliver verified handoffs through the existing shared `Ai_Storage/Hermes/agent-bus/current/` folder; preserve older bundles in `archive/v<version>/`. Do not create more top-level release folders. Supported TrueNAS pull/redeploy is different from Docker restart, which does not pull an image.

## Topic sessions and known limits

The original test topic is `auto-wake-v1-test`. Canonical current membership/ownership is shared knowledge `agent-bus/topics/auto-wake-v1-test.md`; read its latest revision instead of copying stale session IDs. LT-Ops and unrelated topics stay untouched.

Beau selected exact topic conversations instead of a dedicated worker. The unused `mcp-worker` profile was deleted and must not be recreated as a fallback. Never use `--latest`, substitute a personal conversation, impersonate another agent or launch real model/CLI calls as parser probes.

Server push, client reconnection, exact-session automatic wake and reboot recovery are distinct capabilities. Presence cannot prove a desktop chat is ready to act. Goal/task ownership/fencing and evidence-backed structured handoffs remain separate design work; no second scheduler, hosted credential platform, PostgreSQL memory stack or implicit session ACL is introduced by this release.

## Credentials

Use distinct agent bearer credentials and a separate admin credential. Runtime environment/owner-private configuration supplies values; never commit them. Reports, errors, audits and release evidence must omit tokens, bodies, request keys and fingerprints. REST receipt keys are query parameters so the operational route path remains nonsecret; deployment-server access logs need their own appropriate redaction policy.
