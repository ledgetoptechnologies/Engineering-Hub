# Infrastructure page: UI contract and handoff

The dashboard's Infrastructure page (`#/infrastructure`, `#/infrastructure/<device-id>`) shows the
devices the Hub knows about, what its checks last saw, and any maintenance in progress. Nothing on
the page powers, restarts or changes a device.

**Status:** UI built against a fixture. The HTTP routes below are the UI's proposal. Until the
backend freezes them, only `hub-ui/src/infrastructureApi.ts` should need to change. No live data,
access grants or deployment come with this page.

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

## Proposed routes

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

## Needed on the server before this goes live

1. The routes above, or the backend's final ones, so `infrastructureApi.ts` can be updated.
2. The default-deny capability on HTTP and MCP, plus `capabilities.infrastructure` per caller in the overview.
3. The client-route fallback must serve `/infrastructure` and `/infrastructure/<id>`, so path URLs
   work as well as `#/` ones.
4. A backend test that validates `hub-ui/src/fixtures/infrastructure-v1.json` against the real response models.
