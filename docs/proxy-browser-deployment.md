# Hub behind the existing LAN proxy or tunnel

Beau approved replacing loopback-only UI isolation with an authenticated,
network-reachable browser listener. This is a network-boundary change, not a
change to who may sign in. Ops authentication and Project Alpha stay separate.

The default browser bind remains `127.0.0.1`. The explicit operator flag
`--browser-host 0.0.0.0` enables IPv4 proxy reachability. Only numeric IP
addresses are accepted. The operator profile in `compose.hub-unified.yaml`
uses this flag and requires the new release; older images reject it.

| Use | Destination |
| --- | --- |
| Agents and internal MCP | `http://192.168.50.80:8770/mcp` |
| Existing Hub tunnel origin | `http://192.168.50.80:8771` |
| Human-facing URL | `https://hub.ledgetoptechnologies.com` |

There is no 8765 compatibility alias in this profile. Update all three agent
clients deliberately. Do not silently switch clients or change their tokens.

## What protects the UI

The browser frontend validates a signed Cloudflare Access assertion before
serving HTML, assets, sign-in or API routes. It checks the configured signing
keys, issuer, application audience, expiry and exact email allowlist. A Host,
Origin, Referer, forwarding header or email header is not authentication.
An existing Hub session cookie alone cannot bypass this outer check. Existing
human sessions, CSRF validation and permissions remain mandatory where used.
Browser ingress rejects agent bearer credentials and MCP/bus routes; agent
ingress does not serve the dashboard. Health remains an unauthenticated,
non-secret diagnostic endpoint, as before.

This does not claim physical isolation from the LAN. The port is reachable;
requests without a valid Access assertion are denied. A trusted proxy does not
get to invent user identities. An allowed user's valid, stolen JWT is a bearer
credential and must be treated accordingly. No IP allowlist, Nginx shared
secret, new authentication system or Ops dependency is introduced.

The existing bridged cloudflared connector can retain its current networking.
Keep its Hub-only service URL on the NAS address and UI port above. Do not
change Project Alpha's destination, add a second tunnel, or change Ops. An
existing Nginx proxy can also forward to that port, but is not required merely
to make this listener reachable. It must preserve the signed Access assertion;
adding a proxy or changing its headers is a separate operator action.

## Upgrade and acceptance

The Compose tracks tested main publication with `:latest` and `pull_policy:
always`. A plain restart does not fetch a replacement image: explicitly pull
and redeploy through the existing TrueNAS app. Keep the prior image identity
for recovery. Replace credential placeholders privately with the existing
protected values, never token values copied from a transcript.

Only `/mnt/Plugins/App_Data/Engineering-Hub/` is mounted, at `/var/lib/hub`.
The bus database, audit and files belong in its `agent-message-bus` subdirectory;
the existing Hub database stays at the root as `hub.sqlite3`. The old
`/mnt/Plugins/App_Data/Agent_MCP` directory is NOT mounted and is NOT deleted.
The application deliberately refuses missing bus state rather than creating an
empty replacement and claiming that history was migrated.

Stop both old writers and obtain coherent backups/test copies of both state
roots before copying the complete old bus state into that new subdirectory.
Keep the old source intact. Do not overwrite an existing destination: compare
and reconcile it first. Include SQLite WAL/SHM siblings, audit, knowledge/files
and any dependent state, preserve numeric ownership/permissions, and verify
content parity on the stopped copies. Confirm UID568 can access the copied
state; do not recursively chown the source or silently weaken permissions.
Do not perform a destructive move or blindly restore snapshots after new writes.
The profile is not ready to start until these files exist and parity is verified.

Removing the port alias does not make concurrent writers safe. Do not delete either app's
state or restart an old writer beside the new owner. Preserve post-cutover
writes before any rollback and check downgrade compatibility on copies.

After deployment, check both health endpoints and native push health; verify
the allowed human identity through the tunnel, rejection of unauthenticated
LAN UI requests and forged headers, and denial of MCP through the browser
hostname. Verify each agent on 8770, data parity and recovery separately.
Source/container tests are not evidence of actual NAS backup, data parity,
human sign-in, correct-session wake or host reboot recovery.

Historical loopback/alias handoffs remain historical. This document and the
current operator profile supersede their tunnel-host-network/8765 directions.
