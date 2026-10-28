# Revocable agent connections: staging pilot

This optional identity foundation gives each approved agent installation its own
credential and short-lived sessions. It is disabled by default. It does not yet
move existing Hub tasks, project permissions, agent seats or automatic chat wake
onto those identities. Existing family tokens and tools keep their existing
behavior; new session credentials cannot use those legacy tools.

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

On the PC running the agent, choose a private profile directory. An installation
running Codex and one running Claude use separate profiles, even on the same PC.
Run the installed staging helper locally, for example:

```powershell
python -m agent_message_bus.agent_session_client --profile-dir "$env:LOCALAPPDATA/EH-Staging/Codex" enroll --origin https://STAGING-HUB --family codex --name "Codex on processing PC" --host-label processing-pc
```

The helper creates the installation secret locally and sends only its SHA-256
fingerprint for approval. It prints the installation ID, public pairing code and
fingerprint. In staging **Agents → Installations**, compare those values with
the PC's output and approve the matching pending request. Host labels are
self-described metadata, not proof of machine identity. Pending requests expire
after ten minutes. The initial approval lasts 24 hours.
Expired requests that were never approved are reclaimed during subsequent
enrollment so abandoned requests cannot permanently fill the pairing table.
Retrying an expired credential starts a fresh pending request and requires a new
owner approval. Approved identities, session history and audit are retained.

The helper retains the original private identity across a failed enrollment
request; retry the same command instead of generating another credential.
On Windows, private profiles use current-user DPAPI. On other platforms, files
are owner-only mode0600. Windows private-storage behavior still requires a real
Windows acceptance check; Linux fixture success does not prove it.

## Connect a chat session

After approval, create a named local session:

```powershell
python -m agent_message_bus.agent_session_client --profile-dir "$env:LOCALAPPDATA/EH-Staging/Codex" connect --session treedar-test --label "TreeDAR staging test"
```

This stores the short-lived session credential privately and prints only session
metadata and the staging MCP URL. Sessions last at most fifteen minutes. Configure
the app's private credential-helper pipe to invoke the helper's `headers` command
with this same profile and session name. Its stdout contains the bearer header
for the connector; do not paste or record that output in a chat or review.

For a new locally connected Codex HTTP MCP entry, the documented setting is
`http_headers_helper`: a local command returning a JSON header map. Configure
the new entry with the staging MCP URL and an absolute interpreter/helper path.
Do not copy explicit bearer or stored OAuth credentials from a production entry:
those take precedence over a helper's Authorization header. Codex caches headers
per connection; this helper deliberately does not renew a revoked session.
See the [official MCP configuration documentation](https://learn.chatgpt.com/docs/extend/mcp).
Verify support in the installed client and Windows DPAPI before claiming real-PC
acceptance. The existing legacy notification connector does not yet consume
these installation mailboxes.

The header helper does not silently exchange a new session after expiry or
revocation. Reconnect explicitly with a new local session name when intended.
The new `session_*` MCP tools provide identity, peer discovery and isolated
installation mailboxes. They do not grant existing Hub tool, execution or project
access. Native automatic wake into a desktop chat is a separate integration.

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

Keep actual staging-PC rollout, correct-host desktop wake, phone access, restart
and live admission evidence separate from the isolated synthetic test receipt.
