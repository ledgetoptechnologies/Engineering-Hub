# Discovered machine metrics

This source adds schema 3 machine metrics and explicit internal-installation
classification. It is not evidence of an installed release, an accepted real
Prometheus connection, or staging deployment/rollback acceptance.

## Setup and access

When no operator `infrastructure.json` exists and no explicit settings path was
selected, the app uses its configured connection owner (or explicit GitHub owner)
to initialize the ordinary inventory in its existing state directory. The owner
gets the Infrastructure page without creating a settings file. Legacy internal
agents get inventory reads, not automatic write permissions. An existing settings
file takes precedence, including `enabled: false`.
Without either a configured connection owner or an explicit GitHub owner, this
automatic setup stays disabled. Browser metrics also require a known owner.

Discovery needs one trusted literal-IP Prometheus origin. The optional settings
object accepts `{"address":"192.0.2.80","port":9090}` without a target mapping
list; these documentation addresses are not deployment defaults. Existing mapped
refresh remains supported. Built-in owner defaults use
`http://192.168.50.80:30104`, independently verified serving eight active targets
on October 30, 2026 UTC. An explicit operator origin overrides it. No discovery
scan chooses an address. With an explicitly unconfigured origin the owner sees `not_configured`,
not `unreachable`. Reads do not create inventory records or contact scrape URLs.

Browser metrics belong to the configured owner. Unknown ownership does not fall
back to an arbitrary human capability. Legacy internal bearers are authenticated
normally. Per-PC agents use `hub_infrastructure_metrics` through the existing MCP
transport, with the existing exact Host/source-IP restrictions. They additionally
need an owner-maintained `internal` classification. Project-only and unclassified
installations receive no metrics. Classification does not grant project access,
extend session expiry, or resurrect revoked installations. Existing installations
are not silently classified from family, hostname or project grants.

## Owner classification API

All routes use the existing owner browser session; PUT requires its normal CSRF
proof. Scope revisions are independent of installation connection revisions.

| Method | Path below `/api/hub/v1/agent-connections` | Result |
| --- | --- | --- |
| GET | `/installation-scopes` | `{installations: Scope[], truncated: boolean}` for approved installations |
| PUT | `/installation-scopes` | Atomic batch returning `{installations: Scope[]}` |
| GET | `/installations/{id}/scope` | One scope |
| PUT | `/installations/{id}/scope` | One updated scope |

A scope contains `installation_id`, `kind`, `revision`, `updated_by`, `updated_at`
and `internal_effective`. Missing classifications are `unclassified`, revision 0,
with null update provenance. An inactive installation is never effectively
internal. The list excludes pending enrollments and returns at most 256 approved
installations, prioritizing active devices and then newer approvals by creation
time. `truncated` reports omitted history; historical rows do not disable the
list. Individual owner reads remain available for omitted installations.

Single PUT: `{expected_revision, kind}`. Batch PUT:
`{kind, installations: [{installation_id, expected_revision}]}`. Kinds are
`internal`, `project_only`, `unclassified`. A batch accepts 1–256 unique IDs and
rolls back all changes and audit entries if any item fails. Conflicts use
`scope_revision_conflict` (409); invalid input uses `invalid_installation_scope`
(422); pending IDs use `installation_not_approved` (409); an inactive identity
cannot be marked internal (`installation_inactive`, 401).

## Metrics contract

The browser endpoint `/api/hub/v1/infrastructure/metrics` and the MCP tool return
the same schema 3 envelope: `schema_version`, `generated_at`, `source`, `devices`.
Schema 3 adds `net_rx_bytes_per_second` and `net_tx_bytes_per_second` (node and
windows exporters only; null otherwise). The UI accepts schemas 1–3, but older UI
builds reject 3, so the UI and backend must ship in the same release.
Per-PC credentials remain MCP-only; this feature does not open Hub REST to them.
Admission is checked before upstream I/O, after it, and before returning metrics.

Source states are `not_configured`, `ok`, `partial`, `unreachable`. The first and
last contain no device rows. `truncated`, `omitted_count` and a sorted closed
warning set explain incomplete results. A scrape failure is not proof that a
computer is off. Missing values stay null. Target discovery retains unknown
exporters, groups by a namespaced hash of exact validated host labels, and uses
unique fallback keys when host labels are absent. Labels are observations, not
authority or proof of a shared physical machine. Inventory association requires
an exact unique declared name; ambiguous matches remain explicit. That name is
the device's optional `metrics_host` when set (exact, case kept, it replaces the
hostname rather than adding to it), otherwise its hostname.

Reads use only fixed PromQL and the configured origin, with no redirects,
environment proxy routing, arbitrary queries, scheduling or scrape-URL fetches.
The asynchronous fetch deadline is 10 seconds; retained bodies have a 2 MiB
aggregate budget and 1 MiB response budget. These are not hard process-memory or
all-received-network-byte bounds. There are at most 4096 decoded targets, 200
returned targets, 16 disks, 8 GPUs and 10 processes per row.

## Current evidence and remaining work

136 selected offline Linux checks pass, including actual unified ASGI/MCP
transport, bulk CAS/rollback, owner CSRF, project-only denial, sibling isolation
and revocation during a mocked Prometheus fetch. The one bounded test container
was removed after a natural exit. No live keys, grants, NAS settings or production
deployment changed. Full regression, UI integration and installed-system
acceptance remain required before release. Origin reachability is independent
of accepting the new Hub reader against that service.
