# Revocable agent connections: production mode

Production connections reuse the installation and short-session model described
in [the staging pilot](revocable-agent-staging.md). The production mode is an
explicit, default-off configuration. Staging owner identity, databases,
installations and grants do not carry over.

## Runtime contract

Set `HUB_AGENT_SESSIONS_MODE=production` only on the intended Hub deployment.
Supply the existing Hub's exact canonical HTTPS origin, the fixed production
owner actor obtained from that owner's authenticated `/api/hub/v1/auth/me`
response, and a new private session database path. The production owner must be
a Cloudflare Access-backed human actor. The Hub also verifies that the session
origin matches its configured browser origin and that its session database is
separate from Hub and message-bus state. Production mode is refused when the Hub
is not configured for Cloudflare Access or when its agent source-IP allowlist is
empty.

Keep `HUB_AGENT_SESSIONS_HUB_BRIDGE=disabled` unless Hub tools are separately
needed. When enabled, the bridge creates its schema at startup but creates no
grants. The configured human owner grants capabilities to an explicit list of
existing projects for each installation. Installation approval does not grant
Hub access. Project creation and archive remain human-only. Do not copy grants,
database files, owner identities, or credentials from staging.

These variables describe the source contract; they are not an activation-ready
configuration:

```text
HUB_AGENT_SESSIONS_MODE=production
HUB_AGENT_SESSIONS_DB=<new private database path inside the Hub state mount>
HUB_AGENT_SESSIONS_OWNER=<the production owner's authenticated human:cf actor>
HUB_AGENT_SESSIONS_ORIGIN=<the exact production HTTPS Hub origin>
HUB_AGENT_SESSIONS_HUB_BRIDGE=disabled
```

The database is created by application startup. Review the exact state path,
UID/GID ownership, permissions, backup plan, and separation from current Hub and
legacy bus databases before enabling the mode. The mode remains disabled when
`HUB_AGENT_SESSIONS_MODE` is absent.

## Network and browser boundaries

The agent listener serves `/mcp` and `/api/agent-sessions/v1`; the browser
listener serves human pages and owner administration. The production proxy must
route these exact agent paths to the agent listener while keeping browser owner
routes behind the existing Cloudflare Access policy. The application requires
the configured Host and checks the actual client address against its existing
agent allowlist; it does not trust forwarded client-IP headers. Production
requires a nonempty allowlist. Verify the production proxy, tunnel paths, Host
preservation, listener bindings, and source-IP policy before enabling the
session routes. Public bootstrap downloads
contain no credentials; enrollment and session operations retain their
credential and owner checks.

The owner manages installations from the authenticated Hub browser session.
Pairing, approval, session revocation, installation revocation, and Hub project
grants remain distinct operations. Start with no Hub grants; grant only the
projects and capabilities required by a specific installation.

## Lifetime and revocation

The existing defaults remain unchanged: pending enrollment requests expire
after ten minutes, installation approval defaults to 24 hours, and a session
lasts at most 15 minutes. Sessions are not automatically exchanged or renewed.
After session expiry, explicitly connect a new named session and reconnect the
client. An expired approved installation has no renewal operation; it requires
a fresh owner-approved pairing. Revoking a session ends that session; revoking
an installation invalidates all its sessions. Existing family tokens remain a
separate legacy credential path and are not converted or rotated by this mode.

This document does not configure production values or authorize activation.
Production readiness still requires review of the intended release, actual
production browser/Cloudflare and agent-route configuration, the authenticated
owner binding, private database placement, and a separately approved live
onboarding and revocation check.
