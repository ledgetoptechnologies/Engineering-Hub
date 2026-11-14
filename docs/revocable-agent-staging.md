# Revocable agent connections: staging pilot

This optional identity foundation gives each approved agent installation its own
credential and short-lived sessions. It is disabled by default. Existing family
tokens and tools keep their existing behavior; session credentials cannot use
generic legacy tools. The separate, default-disabled [session Hub authorization
bridge](session-hub-authorization-bridge.md) can grant an installation a finite
set of Hub metadata capabilities for explicitly selected projects. Connection
approval alone grants no Hub rights. Execution, generic legacy access and
automatic chat wake remain separate and unavailable through that bridge.

This document covers staging only. Production mode has separate owner, origin,
database, network and onboarding requirements described in
[revocable-agent-production.md](revocable-agent-production.md).

## Operator setup

Use a new isolated Hub instance with new Hub, bus and session databases, synthetic
legacy credentials and dedicated private listeners. Do not reuse the live
container environment, token files, volumes, existing subscriptions or staging
TEST/Engine. The session database is separate from the legacy bus database.
When enabled, the `ehi_`, `ehs_` and `ehc_` credential prefixes are reserved
(case-insensitively). A conflicting legacy credential makes staging startup
fail before creating state; it is never silently reinterpreted or rotated.

The explicit configuration is:

```text
HUB_AGENT_SESSIONS_MODE=staging
HUB_AGENT_SESSIONS_DB=<absolute private staging session database path>
HUB_AGENT_SESSIONS_OWNER=<the authorized human actor from staging sign-in>
HUB_AGENT_SESSIONS_ORIGIN=<the staging Hub's exact canonical origin>
```

The owner must sign in through the staging Hub's normal human authentication.
For initial setup, bring up the isolated Hub with this feature disabled, sign in,
and obtain only the `actor` field from its authenticated
`/api/hub/v1/auth/me` response. Use that actor for the owner setting before
enabling the feature. Do not copy the response's CSRF token or browser cookie
into configuration, logs or a chat. The database retains its original owner;
changing an environment label cannot transfer ownership.
In a fresh browser, open the explicit `/api/hub/v1/auth/login` path and sign in
as that owner before opening a deep link such as `/agents`. Hub sign-out clears
the Hub's local session; it does not sign out the browser's global Cloudflare
Access identity. Verify the actual signed-in account rather than assuming a
deep link or Hub sign-out switched it; do not clear unrelated Access sessions.
Approval, rename and revocation require that exact owner plus CSRF verification.
Other allowlisted humans and every agent bearer are refused owner actions.
Use HTTPS for access from another PC. Plain HTTP is accepted only on loopback.
The deployment needs one owned bus process and its exact session repository;
cross-process stream invalidation is not established by this pilot.

The current configuration uses one canonical HTTPS origin for both front doors.
Its path router must send `/mcp`, `/mcp/` and
`/api/agent-sessions/v1` (including descendants) to the agent listener. Send
browser assets, human authentication and `/api/hub/v1/agent-connections` to
the Access-protected browser listener. Preserve the canonical Host and the
existing source-IP restrictions. Sending everything to the browser listener
will refuse enrollment and MCP. Separate browser and agent origins are not
supported by this pilot configuration. Enrollment and exchange must not redirect
to an interactive login page: the helper intentionally refuses redirects.

## Pair each installation

With the session module enabled, the agent listener provides two read-only
bootstrap downloads without a bearer: `GET`/`HEAD`
`/api/agent-sessions/v1/client/manifest` and
`/api/agent-sessions/v1/client/agent_session_client.py`. These exact routes retain
canonical Host, actual source-IP and path checks; all other session APIs retain
their existing authorization. Disabled instances return 404. The manifest gives
`schema_version`, `helper_version`, `bytes`, `sha256` and the same-origin relative
`url` of the fixed packaged helper. Downloads do not create an installation,
session or grant, and never include credentials or configuration.

An agent can use an already installed Python interpreter: fetch the manifest and
helper over the approved HTTPS staging origin with a descriptive application
User-Agent, verified TLS and no redirects, save to a new local file, and compare
its byte length and SHA-256 with the manifest **before** running it. Do not
download-and-execute a shell pipeline, follow an interactive login redirect, or
overwrite an existing helper/profile. A checksum from that HTTPS origin binds
the fetched bytes to its manifest; it is not an independent release signature.
The helper uses Python's standard library, so this path needs no package-registry
installation. Downloading is self-service; installation approval below is still
the human owner's separate action. This does not configure an MCP client,
enable native wake. The verified Windows and two-PC scope is described below.

On the PC running the agent, choose a private profile directory. An installation
running Codex and one running Claude use separate profiles, even on the same PC.
Run the staging helper locally, for example using an absolute path to the
reviewed `agent_session_client.py` file and an installed Python interpreter:

```powershell
& 'C:/Example/Python/python.exe' 'C:/Example/Engineering-Hub/src/agent_message_bus/agent_session_client.py' --profile-dir 'C:/Example/EH-Staging/Codex' enroll --origin https://STAGING-HUB --family codex --name "Codex on processing PC" --host-label processing-pc
```

The helper creates the installation secret locally and sends only its SHA-256
fingerprint for approval. It prints the installation ID, public pairing code and
fingerprint. In staging **Agents → Installations**, compare those values with
the PC's output and approve the matching pending request. Host labels are
self-described metadata, not proof of machine identity. Pending requests expire
after ten minutes. New UI device approvals last until revoked.
Expired requests that were never approved are reclaimed during subsequent
enrollment so abandoned requests cannot permanently fill the pairing table.
Retrying an expired, never-approved request with its saved credential starts a
fresh pending request and requires owner approval. Approved identities, session
history and audit are retained. This pilot has no renewal action for an expired
previously approved identity: use an explicitly chosen new private profile and
fresh owner approval, preserving the old profile and history. Revoked identities
remain revoked; the helper does not automatically reenroll or rotate credentials.

The helper retains the original private identity across a failed enrollment
request; retry the same command instead of generating another credential.
On Windows, private profiles use current-user DPAPI. On other platforms, files
are owner-only mode0600. A real Windows helper and MCP SDK check verified
current-user DPAPI decryption and private header delivery for that test profile.

## Connect a chat session

After approval, create a named local session:

```powershell
& 'C:/Example/Python/python.exe' 'C:/Example/Engineering-Hub/src/agent_message_bus/agent_session_client.py' --profile-dir 'C:/Example/EH-Staging/Codex' connect --session treedar-test --label "TreeDAR staging test"
```

This stores the short-lived session credential privately and prints only session
metadata and the staging MCP URL. Sessions last at most fifteen minutes. Configure
the app's private credential-helper pipe to invoke the helper's `headers` command
with this same profile and session name. Its stdout contains the bearer header
for the connector; do not paste or record that output in a chat or review.

The following paths, names and origin are illustrative placeholders, not accepted
installation paths. Replace them with the actual absolute paths, staging origin,
profile and session on that PC. These examples use paths without spaces; verify
quoting with the installed client if actual paths contain spaces. Add only a new
staging entry and preserve existing live entries. Do not run `headers` in a
terminal or log its stdout: it contains the bearer credential and is intended
only for the client's private helper pipe.

For a locally connected Codex HTTP MCP entry, the documented
`http_headers_helper` is a command returning a JSON header map:

```toml
[mcp_servers.engineering_hub_staging]
url = "https://STAGING-HUB/mcp"
http_headers_helper = "C:/Example/Python/python.exe C:/Example/Engineering-Hub/src/agent_message_bus/agent_session_client.py --profile-dir C:/Example/EH-Staging/Codex headers --session treedar-test"
```

Do not copy explicit bearer or stored OAuth credentials from a production entry:
those take precedence over a helper's Authorization header. Codex caches headers
per connection. See the [official MCP configuration documentation](https://learn.chatgpt.com/docs/extend/mcp).

For Claude Code, use a separate Claude profile and session: enroll that profile
with `--family claude`, then run `connect` with the same profile directory.
Its documented HTTP `headersHelper` accepts the same JSON
header map. This example shows a new entry in an MCP configuration JSON object:

```json
{
  "mcpServers": {
    "engineering-hub-staging": {
      "type": "http",
      "url": "https://STAGING-HUB/mcp",
      "headersHelper": "C:/Example/Python/python.exe C:/Example/Engineering-Hub/src/agent_message_bus/agent_session_client.py --profile-dir C:/Example/EH-Staging/Claude headers --session treedar-test"
    }
  }
}
```

Claude Code runs this command in a shell, subject to its configuration scope and
trust rules, and invokes it again on reconnect or an authentication retry. See
the [official Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).
An isolated installed Codex 0.165.0-alpha.1.2 app-server test used
`http_headers_helper` and verified `session_identity`. It did not run a model
turn, change the current desktop configuration, or verify native wake. Claude
Code native-client acceptance remains unverified. The existing legacy notification
connector does not yet consume these installation mailboxes.

The header helper does not silently exchange a new session after expiry or
revocation. Sessions last at most fifteen minutes; this is not a permanent
sign-in. Connect explicitly with a new local session name, update that entry's
helper command to use it, and reconnect the client when intended.
The new `session_*` MCP tools provide identity, peer discovery and isolated
installation mailboxes. The optional bridge grants only its explicitly selected
Hub metadata capabilities and projects; it does not grant execution or generic
legacy tools. Native automatic wake into a desktop chat is a separate integration.

Session MCP transports use a separate SDK session manager from legacy family
transports. The installation manager admits at most 1,024 concurrent SDK
transports, and separately bounds registered owners plus opening requests.
Successful client DELETE frees a transport slot; failed initialization is
discarded by the SDK. An expired or revoked credential immediately loses
access, but its SDK transport can occupy its isolated slot until the SDK's
idle cleanup (30 minutes by default); it does not consume legacy transport
capacity. At capacity, new EHS initialization can fail until a slot is freed.
Transport-owner revocation callbacks, active response guards, and attention
streams use separate bounded listener pools, so idle transport owners cannot
consume the response-guard reserve. One session may hold at most 32 transport
owner callbacks, 4 response callbacks, and 4 attention callbacks; the global
limits are 1,024, 128, and 32 respectively. MCP transport and response-guard
capacity exhaustion returns 429; an attention stream may close if its listener
cannot be acquired. Neither means the credential is invalid. A revoked or abandoned SDK session may still
occupy its isolated SDK slot until disposal or idle cleanup.
Clients must create a fresh authenticated transport after expiry or deletion;
this does not restart a chat, replay a write, or establish automatic wake.

## Verified staging scope

A physical Windows and Linux test exchanged messages in both directions and
verified installation mailbox and session claim isolation. It also verified
whole-installation revocation while the Linux installation remained usable,
followed by an owner-approved Windows replacement installation. These results
are limited to that staging test; they do not establish native desktop wake or
project grants. Session-only revocation and closure of an existing stream on
revocation or expiry remain unverified on the real clients.

## Revocation test

Create two same-family installations, A and B, and two sessions under A. Verify
that A and B have separate installation mailboxes. Sessions within A share its
mailbox, but each claim belongs to the exact claiming session: A's sibling
session and B must both be unable to finish that claim. This foundation does not
route installation messages to a particular desktop chat.
Revoke one A session in the owner UI: it must lose access while A's sibling and B
remain usable. Then revoke installation A: all its sessions and future exchanges
must be refused while B continues working. Existing authenticated streams must
close on revocation/expiry; future MCP transport reuse must fail.

Revocation fences Hub access and claim completion. It does not promise to stop
a process or accepted job on the remote computer. Claims are not automatically
reassigned; an explicit reclaim can occur after their lease expires. Distinct PCs,
credentials or sessions alone do not establish independent reviewer authority.

Keep correct-host desktop wake, phone access, restart and live admission evidence
separate from the verified staging-PC and isolated synthetic test results.


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

The existing 16-active-session limit remains. Quotas count active sessions and
those ended in the last 24 hours: 256 per installation and 4096 globally. A
continuous device can reconnect beyond the old all-time history limit without
another pairing. A burst still receives a limit response until its window ends.
The owner session list uses that same window and the existing response shape.

Historical session and audit rows are not deleted or rewritten, so old claim
provenance and recovery remain intact. These stored rows grow over time; the
quota bounds recent activity, not total database size. An operator-reviewed
archive/size-monitoring policy remains a separate maintenance decision. An old
image counts all rows and may refuse new sessions or fail to display more than
256 historical rows after rollback; restoring the newer image preserves state.
