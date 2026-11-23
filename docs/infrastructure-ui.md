# Infrastructure page: UI contract and handoff

The dashboard's Infrastructure page (`#/infrastructure`, `#/infrastructure/<device-id>`) shows the
devices the Hub knows about, what its checks last saw, and any maintenance in progress. Nothing on
the page powers, restarts or changes a device.

**Status:** UI and HTTP/MCP adapters are integrated. The backend checks the complete reference
fixture against its real response projection; Linux app tests exercise authentication, CSRF,
capabilities and persistence. Browser tests exercise the page at desktop and phone widths.
No live data, access grants or deployment come with this source change.

## Access: the current limit

Every signed-in person is global today: there is one Cloudflare Access allowlist and no per-project
human scope. Adding a client collaborator (for example LandScript-only) to that list would give them
everything, so the page must not rely on "any signed-in person".

- The server enforces dedicated, default-deny infrastructure capabilities (read, device write,
  maintenance write) on every HTTP route and MCP tool.
- The nav item shows only when `overview.capabilities.infrastructure === true` for this caller, or
  in demo mode. Hiding the item is a convenience, not the enforcement.
- If the server answers 403, 404, 503 or `gateway_route_forbidden`, the page says only
  "Infrastructure isn't available to you on this Hub." It never reveals whether any devices exist.
- 401 signs the workspace out, as on other pages.

## Routes

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/hub/v1/infrastructure/devices[?include_retired=true]` | none | `{items: Device[], capabilities}` |
| GET | `/api/hub/v1/infrastructure/devices/{id}` | none | `{device, maintenance: {items, truncated}, capabilities}` |
| PUT | `/api/hub/v1/infrastructure/devices/{id}` | `{name, hostname, addresses, network, vlan, role, retired, expected_revision}` | `Device` |
| POST | `/api/hub/v1/infrastructure/devices/{id}/maintenance` | `{reason, expected_end, client_request_id}` | `Maintenance` |
| POST | `/api/hub/v1/infrastructure/maintenance/{id}/resolve` | `{expected_revision, resolution}` | `Maintenance` |

- Every write needs the `X-Hub-CSRF` header.
- `expected_revision` is 0 when creating a device.
- Retiring a device, or returning it to service, is the same PUT with `retired` changed.
- `capabilities` is `{device_write, maintenance_write}` for the caller and controls which forms appear.

## Data shapes

`hub-ui/src/fixtures/infrastructure-v1.json` is the reference. The parser refuses extra fields.

- **Device:** `{id, name, hostname|null, addresses[], network|null, vlan|null, role, retired, revision, updated_at, observations[], open_maintenance|null}`.
  - Declared facts and observations stay separate; there is no combined "online" field.
- **Observation:** `{probe_id, source_label|null, state: "ok"|"failed"|"unknown"|"stale", observed_at|null, valid_until|null}`.
  - The backend maps healthy to `ok` and unhealthy to `failed`.
  - The raw probe ID and the original times must be kept.
- **Maintenance:** `{id, revision, device_id, reason, expected_end|null, state: "open"|"resolved", opened_at, opened_by, resolved_at|null, resolved_by|null, resolution|null}`.
  - Actors are `{kind: "human"|"agent", identity}`.
  - A resolved entry has all three resolution fields set; an open entry has none.

## What the page does

- **Reading:** reads on open, on Refresh, and when the existing event feed refreshes the workspace.
  It never polls.
- **Ageing:** badges age every 15 seconds by the browser's clock.
  - A check past its `valid_until`, or marked `stale`, shows as "Out of date · last seen …".
  - A check that was never run shows "Unknown: no current check". Neither is worded as up or down.
- **Check wording:**
  - "Check passed" and "Last check failed" describe the check only.
  - The device page adds: "A passed check … doesn't prove the device is powered on or that agents can use it."
- **Maintenance:**
  - Passing the expected end makes an open note "overdue". It never closes the note or implies the
    device is back.
  - Reasons are plain text, never markup.
  - Agents appear by name, the signed-in person as "You", and other people as "Another person",
    without showing their identity key.
- **Writes:** each maintenance note keeps one `client_request_id` across retries. When a save gets
  no answer, the page says it may or may not have been saved and keeps the text.
  - `revision_conflict`, `maintenance_already_open`, `idempotency_conflict` and `device_retired`
    each get plain wording and a Reload.
- **Owner editor:** checks the ID, hostname, IP addresses, CIDR network (no host bits; addresses
  inside it) and VLAN (1 to 4094) before saving. The server stays the authority. The VLAN is never
  guessed from an address.

## Machine health cards and drill-down

The Machine health overview shows one compact, keyboard-focusable card per discovered metrics group,
including machines not yet associated with an inventory device. Its default display-name priority is
Main PC, Cluster PC, Hermes, Staging, Sync Pi and DNS; matching is exact after case and common
punctuation normalization, and all other names follow alphabetically. **Customize order** saves only
normalized display names in this browser, scoped to the live human actor; demo preferences are separate.
It does not save host keys, change inventory associations, or affect another actor or demo.

The overview reads current metrics only; it does not fetch history. The same machine workspace
appears on registered-device and discovered-machine detail pages: the machine and time selectors
sit above history charts, followed by current exporter readings and any authorized Inventory details.
The machine selector uses the overview's browser-local order. The current snapshot stays in memory
while switching within Infrastructure; history requests are limited to the selected detail and range.

After a successful Inventory read, a matched card or picker choice opens the registered device.
If multiple opaque groups match one device, `?host=` selects only a group actually bound to that
device. Without an established Inventory read, a metrics-authorized visitor switches between opaque
machine routes without reading Inventory. A missing target is unknown. Older mapped-only metrics
still show current readings but their display keys do not support history. Neither route grants
access; existing checks and maintenance controls keep their separate permissions.

The default is **Last 24 hours**. The fixed options are 1 hour, 6 hours, 24 hours, 7 days and 30 days.
`?range=7d` (or the equivalent hash query) restores the selection on reload and Back/Forward; the
picker carries it to the next machine. Missing samples appear as gaps. The existing history endpoint
now accepts `30d`: it samples every six hours (121 samples including both ends, ending at the most
recent hour). CPU and network rates use the same one-hour window as the 7-day range; it is not a
six-hour average. Available history depends on Prometheus retention and scrape coverage.

## Activation

The optional module remains disabled until the owner supplies explicit infrastructure settings.
See [infrastructure-registry.md](infrastructure-registry.md) for its fixed storage layout and
capabilities. The existing Hub Compose and Cloudflare login stay as they are. Importing declared
devices and connecting an actual observation source are separate steps; the fixture is never
imported as live inventory. No Prometheus collector or new background polling is activated here.
