# Infrastructure registry source contract

`engineering_hub.infrastructure` supplies a standalone, dedicated SQLite ledger,
composed optionally at application startup with capability-gated HTTP, MCP and dashboard adapters.
Adding the module does not provision access, import inventory, probe machines or
change existing Cloudflare policies. Production still lacks project-only human
access; adding a collaborator to its global allowlist would not isolate them.

## Facts, observations and maintenance

Device IDs are stable lowercase slugs, independent of IP addresses. A device has
a display name, declared hostname (nullable), IP literals, optional explicit
network/VLAN, role, retirement state and revision. There are at most 256 devices,
including retired records. Unknown addresses stay empty and unknown network facts
stay null; a VLAN is not inferred from an IP octet. Updates use revision CAS.
Retirement does not erase device history.

Probe observations are separate from declared facts. Each stable probe ID has a
source label, authenticated reporting principal, actual observation/expiry times
and the registry's recording time. Future, equal or older observations for that
same probe are rejected. Missing or malformed provenance is unknown; expiry
becomes stale without changing stored timestamps. Independent probes do not
overwrite each other. A successful exporter check is not evidence of PC power,
application health or agent readiness.

Retain the newest 16 observations per probe and at most 16 probe IDs per device.
Insertion and removal of only that probe's excess older rows share a transaction.
This rolling telemetry history does not delete maintenance or idempotency records.

Maintenance records use the authenticated actor, reason, recording time and an
optional expected end. One record may be open per device. The same actor/request
key and exact payload returns its original result; changed payload conflicts.
Resolution is explicit with revision CAS, actor, time and explanation. Neither
elapsed expected time nor a successful probe resolves it automatically. Reasons
and explanations are bounded plain text with newlines; rendering must be inert.

Maintenance and audit caps fail closed rather than silently dropping evidence.
The event budget reserves space to resolve an already-open maintenance entry.
History reads are bounded to 50 and report truncation. Long-term evidence
compaction/export requires a separately specified retention contract.

## Authority and integration boundary

Trusted adapters construct a typed human/agent principal from real authentication;
request bodies cannot establish identity. Every operation requires an explicit
read capability. Device editing additionally requires a human device-write grant;
maintenance and observation reporting have separate grants. Unknown principal
kinds and unlisted identities are denied. No actual grants are created here.

The constructor takes ownership of an idle, dedicated SQLite connection. It
rejects foreign/incomplete schemas, temporary objects and attached databases
before initialization. Do not pass a HubRepository connection: future shared
ledger integration must use its writer-fenced transactions instead of bypassing
them. Any connection factory used across threads must select SQLite's appropriate
threading configuration; the registry lock alone does not change that setting.

The adapters enforce the same default-deny capability on every
HTTP and MCP list/read/write route, require human CSRF for mutations, and expose
caller-specific UI capabilities. Client/project-only collaborators must not
inherit global infrastructure access. UI hiding is not authorization. Default
configuration remains disabled until the owner provisions the intended policy.

The UI shows declared facts, each probe's age/source and the maintenance journal,
with manual refresh and existing event invalidation. It ages observations locally
and never manufactures a new last-seen timestamp from a cached read. Invalidation
uses only a generic refresh event; device details and reasons do not enter the
global Hub event payload.

## Optional startup settings and storage

The existing state mount is sufficient. Place `infrastructure.json` beside the Hub
database; no Compose changes, new token or Cloudflare setting is needed. The file
is bounded to 16 KiB and uses this closed schema (identities below are examples):

```json
{"schema_version":1,"enabled":true,"principals":[
  {"kind":"human","identity":"human:42","capabilities":["read","device_write","maintenance_write"]},
  {"kind":"agent","identity":"hermes","capabilities":["read","maintenance_write","probe_write"]}
]}
```

Use the actual authenticated human identity, never a guessed email or display name.
Every writer also needs `read`. Agents cannot receive `device_write`. Unlisted
callers have no access; being signed into another project grants nothing here.
Settings are read on startup, with no automatic credential or grant provisioning.
On Linux the regular, single-link configuration file must be root/current-user
owned and not group/other writable. Keep the mounted state paths stable while live.

Missing or disabled settings create no inventory directory/database. Valid enabled
settings create a private `infrastructure/` directory and dedicated `inventory.sqlite3`
under that state root (0700/0600 on Linux). A lifetime owner lock prevents a second
module writer. Foreign schemas, symlinks, changed live storage identity and existing
SQLite journal/WAL/SHM files fail closed. Existing journals require reviewed recovery;
the module does not delete them or silently adopt another writer's database.
Invalid optional settings leave the rest of Hub running with infrastructure unavailable.

Five MCP tools expose list/get, maintenance open/resolve and probe record under the
authenticated agent's capabilities. Probe recording accepts actual observation and
expiry times and performs no network probe. There is no collector, arbitrary target URL,
power control or automatic shutdown. Reconcile Hermes's historical device facts before
import; old manual status labels are not current observations.
