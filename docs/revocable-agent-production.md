# Revocable agent connections: production mode

Production connections reuse the installation and short-session model described
in [the staging pilot](revocable-agent-staging.md). The production mode is an
explicit, default-off configuration. Staging owner identity, databases,
installations and grants do not carry over.

## Runtime contract

Set `HUB_AGENT_SESSIONS_MODE=production` only on the intended Hub deployment.
Supply the existing Hub's exact canonical HTTPS browser/owner origin, the fixed
production owner actor obtained from that owner's authenticated
`/api/hub/v1/auth/me` response, and a new private session database path. The
production owner must be a Cloudflare Access-backed human actor. The Hub verifies
that the owner origin matches its configured browser origin and that the session
database is separate from Hub and message-bus state. Production mode is refused
when the Hub is not configured for Cloudflare Access or when its agent source-IP
allowlist is empty.

For laptop agents that must use only a VPN or private LAN, set the optional
production-only `HUB_AGENT_SESSIONS_PRIVATE_ORIGIN` to the exact listener origin
using `http://` and a literal RFC1918 IPv4 address with an explicit port. For
example, `http://192.168.50.80:8770`. The browser/owner origin remains HTTPS and
is not replaced. When configured, the private origin is the selected session/MCP
transport origin for profiles enrolled in private-VPN mode. That helper mode
requires an explicit `--private-vpn` flag during enrollment and again when
connecting; it never falls back to the HTTPS origin. Leaving the setting absent
preserves the existing HTTPS transport for compatibility. Do not use that default
for laptop profiles required to stay off the public Internet.

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
HUB_AGENT_SESSIONS_PRIVATE_ORIGIN=<optional private http://RFC1918-IPv4:port listener>
HUB_AGENT_SESSIONS_HUB_BRIDGE=disabled
```

The database is created by application startup. Review the exact state path,
UID/GID ownership, permissions, backup plan, and separation from current Hub and
legacy bus databases before enabling the mode. The mode remains disabled when
`HUB_AGENT_SESSIONS_MODE` is absent.
`HUB_AGENT_SESSIONS_PRIVATE_ORIGIN` is production-only; unset it when disabling
production sessions or switching to staging. The server refuses disabled or
staging mode if that variable remains configured.

## Network and browser boundaries

The browser listener serves human pages and owner administration over the
existing HTTPS origin and Cloudflare Access policy. Keep those browser routes and
the owner sign-in unchanged. The agent listener serves `/mcp` and
`/api/agent-sessions/v1`. When private-origin mode is configured, route those
agent paths only to the exact private listener over the VPN/private LAN; do not
publish an Internet route or add a Cloudflare Access bypass. Cloudflare Access is
not required on the private agent path. The source-IP policy bounds enrollment;
approved installation and short-session credentials authorize subsequent access.

The application requires the configured exact Host and checks the actual client
address against its existing agent allowlist; forwarded client-IP headers are
not trusted. Production requires a nonempty allowlist. The private-origin
validator accepts only canonical `http://` RFC1918 IPv4 literals and explicit
ports; it rejects DNS names, public/reserved IP ranges, IPv6, loopback, and
malformed URLs. A private IP address alone does not prove that a connection
arrived over VPN. The operator must bind/firewall the listener to the intended
private interface and verify VPN routes and source-IP policy. Plain HTTP is
acceptable only across the trusted VPN/private network; never expose it to the
public Internet. Bootstrap downloads contain no credentials. Enrollment
requests are intentionally pre-authentication: the exact Host and actual source-IP
allowlist, request limits, and pending-pairing bounds apply, and a request alone
grants no approved installation authority, session, or Hub access. Exchange requires the installation
credential after owner approval; active session APIs require the short-lived
session credential.

For each laptop profile, enroll with the HTTPS owner origin plus explicit
private-VPN selection and private listener origin, then connect with the same
selection. The helper stores that selection in the protected installation
profile and prints the private MCP URL. A private profile refuses `connect`
without `--private-vpn`; it will not retry or fall back to the HTTPS browser
origin. Existing profiles keep their existing transport behavior only while the
server's private-origin setting is absent. Once configured, that private Host is
required for new enrollment, exchange, and session MCP requests; existing
HTTPS-origin profiles cannot connect through their old URL. Use a separate
profile directory and complete a new owner-approved pairing in private mode;
leave existing credential files unchanged.

```text
agent_session_client.py --profile-dir <private-profile> enroll --origin <https-owner-origin> --private-vpn --private-origin <http-rfc1918-ip:port> --family <family> --name <installation-name> --host-label <laptop-label>
agent_session_client.py --profile-dir <private-profile> connect --session <session-name> --private-vpn --label <session-label>
```

The owner manages installations from the authenticated Hub browser session.
Pairing, approval, session revocation, installation revocation, and Hub project
grants remain distinct operations. Start with no Hub grants; grant only the
projects and capabilities required by a specific installation.

## Lifetime and revocation

Pending enrollment requests expire after ten minutes. New owner-UI approvals
last until revoked; the legacy API approval default remains 24 hours. A session
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


## Device approval lifetime

New owner-UI approvals last until the owner disconnects/revokes that installation.
Pairing requests still expire after ten minutes. Sessions still expire after
15 minutes and reconnect explicitly; persistent device approval is not an
unbounded bearer session or an automatic renewal service. Project grants remain
separate and time-limited. Existing timed approvals keep their exact deadlines.
For an active device, the owner can choose **Keep approved…**, verify the
computer/fingerprint, and confirm **Keep approved until revoked** once. This
never restores an expired or revoked installation.

The explicit API option is `until_revoked: true` on owner approval; do not send
`lifetime_seconds` with it. Older clients omitting this option retain their
previous bounded approval semantics. Owner authentication, CSRF, revision
checks, audit, and installation revocation still apply. No legacy key changes
or NAS permission/storage changes are required.

Storage uses `expires_at=0` only on already-approved persistent records, exposed
as a null installation expiry. Sessions and Hub grant expiries remain finite.
Older images treat these records as expired, so rollback fails closed for those
devices until the newer image is restored. Never run mixed versions against the
same session database. No records are silently extended during upgrade.

Session-history quotas remain bounded (256 retained sessions per installation,
4096 globally); persistent approval does not remove those limits. Exhaustion
needs a separately reviewed retention/recovery solution, not re-pairing or
blind deletion of old sessions referenced by claims.
