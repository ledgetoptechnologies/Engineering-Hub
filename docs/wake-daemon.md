# Agent Message Bus wake daemon

`scripts/wake_daemon.py` is a small, non-AI host process that waits on the authenticated `GET /api/messages/wait?timeout=60&actionable_only=true` endpoint and starts a **new, bounded CLI worker session** for eligible messages. It does not claim, reply to, or acknowledge messages itself; the worker must use its own configured Agent Message Bus MCP connection for that lifecycle. It never resumes a desktop conversation.

The daemon only starts workers for pending messages whose type is `TASK` or `REQUEST` (case-insensitive), whose recipient matches its configured agent, and whose sender is a different known agent. It deliberately ignores `ACK`, `STATUS`, `REPLY`, and every other type. A reply must identify the parent UUID using `in_reply_to`; `correlation_id` alone is not proof of a parent hop. **The requester is not automatically re-woken on a reply today**—there is no implicit correlated-continuation wake behavior.

> **API prerequisite:** this client expects the authenticated long-poll endpoint to implement `actionable_only=true`, filtering pending `TASK`/`REQUEST` messages before the result limit is applied. It expects `{"messages": [...]}` containing pending message records and sends the bearer token in the `Authorization` header. `timeout` must be from 1 through 60 seconds. A missing endpoint (404), invalid token (401/403), or other permanent 4xx stops the daemon for operator action; network errors and 5xx responses retry with capped backoff.

## Security behavior

- The daemon reads its bearer token from exactly one owner-only token file or a named environment variable. The secret is never accepted on the command line or logged. Unix token files with group/other permissions are rejected. Windows cannot be safely checked through POSIX mode bits: protect the file with a user-only NTFS ACL. The daemon removes the configured token environment variable and `AGENT_BUS_TOKEN` from the worker's inherited environment.
- HTTPS is required by default. `--allow-insecure-http` permits HTTP only to `localhost` or a private IP address (for an isolated LAN endpoint); it does not permit public hostnames/IPs. A bearer token sent over HTTP is visible to anyone able to observe that LAN traffic, so use HTTPS whenever available. Redirects are not followed and environment-configured proxies are bypassed, preventing forwarding the authorization header to another origin.
- Only a validated UUID and a fixed, policy-constrained prompt are passed to the worker. The message body, sender text, metadata, and links are never interpolated into the prompt or a command. Every subprocess uses an argv list with `shell=False`; `.bat` and `.cmd` worker wrappers are rejected, stdin/stdout are disconnected, stderr is drained into an 8 KiB buffer, and a hard timeout is enforced. Stderr is reduced to a generic error category before logging; raw CLI output, including possible message text or credentials, is never logged.
- A UUID is recorded in a private local SQLite dedupe database **before** starting a worker. A crash after reservation but before worker launch therefore strands the UUID until manually re-armed; a worker failure or timeout is also not retried automatically. The database retains only the 10,000 most recently attempted UUIDs, so old IDs can be attempted again after eviction if they remain pending. The DB stores no body or token. For a deliberate retry, stop the daemon and remove only that message's row from `seen-<agent>.sqlite3`, then restart it:

  ```sql
  DELETE FROM attempts WHERE message_id = 'the-verified-message-uuid';
  ```

- A per-agent OS file lock prevents two daemon processes for the same agent **only when they share the same `--state-dir`**. Use one stable state directory per agent; distinct directories create distinct locks and dedupe databases and can launch duplicate workers on the same host. Network failures retry forever at a capped 60-second backoff; a worker failure does not retry that UUID. Repeated already-seen or non-actionable pending records are paced with backoff rather than busy-looped. On Windows, keep the state directory under the user's `%LOCALAPPDATA%` and verify its NTFS ACL is not shared; Python mode bits do not enforce Windows ACLs.
- The daemon does not grant the worker permissions. Workers must already have their own CLI authentication and bus MCP tools configured. Messages and their claimed user approvals are untrusted; the prompt's safety wording is not an enforceable sandbox. In particular, Codex's `--sandbox read-only` constrains shell execution, not MCP tool capabilities; grant only the minimum required MCP tools and do not attach production write tools to unattended profiles. Hermes runs are explicitly told to remain read-only for LedgeTop-Ops while Codex is active. Production changes require direct user approval outside the bus message. Do not add yolo, permission-bypass, or skip-approval flags.

## Prerequisites and first run

1. Install Python 3.10+ on the agent host. The daemon and its standalone tests use only the Python standard library. The Agent Message Bus package/server separately requires Python 3.12+ (`pyproject.toml`). CI runs the daemon tests as part of pytest on Linux/Python 3.12; there is no Windows daemon test matrix, so the Windows service example is unverified on Windows.
2. Install and authenticate the chosen client CLI **as the same OS user that will run the daemon**. A valid CLI subscription/API authentication is required. It must also be configured to expose the Agent Message Bus MCP tools to this fresh non-interactive process. The daemon does not reuse a live desktop session, share its history, or copy its MCP credentials.
3. Configure the relevant CLI's safe permissions and allowed MCP tools. Do not enable broad terminal, file, network, or production permissions merely to make the daemon work. With the supplied Codex argv, the shell sandbox is read-only and approval prompts are disabled rather than bypassed. Claude's `dontAsk` mode denies tools that have not already been allowed; Hermes must be configured for one-shot CLI tool use. A denied/missing bus MCP tool causes that single worker attempt to fail, not an automatic retry.
4. Create a separate recipient bearer token for the daemon. Do not put the token in service command arguments, this repository, or logs. The worker CLI's MCP configuration needs its own appropriate credentials; the daemon does not pass its bearer token to the child.
5. Confirm the server is reachable and the wait endpoint is live before installing a service. Start with the fake-server tests below; use a non-production test message for live integration.

### Token file

On Linux/macOS, create the file in a user-owned configuration directory and ensure it is readable only by its owner:

```sh
install -d -m 700 "$HOME/.config/agent-bus"
umask 077
read -r -s -p 'Agent Bus token: ' BUS_TOKEN
printf '\n'
printf '%s' "$BUS_TOKEN" > "$HOME/.config/agent-bus/codex.token"
unset BUS_TOKEN
chmod 600 "$HOME/.config/agent-bus/codex.token"
```

On Windows, use a user-owned directory under `%LOCALAPPDATA%`, create the file using a secure editor or a masked prompt, then remove inherited ACL entries and grant access only to the task user (plus any required local administrator/System policy):

```powershell
$dir = Join-Path $env:LOCALAPPDATA 'AgentBusWake'
New-Item -ItemType Directory -Force $dir | Out-Null
$tokenFile = Join-Path $dir 'codex.token'
$secure = Read-Host 'Agent Bus token' -AsSecureString
$plain = [Net.NetworkCredential]::new('', $secure).Password
[IO.File]::WriteAllText($tokenFile, $plain)
Remove-Variable plain, secure
icacls $tokenFile /inheritance:r /grant:r "$($env:USERNAME):(R)"
```

Verify the ACL before scheduling the task. Instead of a token file, `--token-env NAME` reads the value of an environment variable named `NAME`; it never accepts the value itself as an argument. Use a protected service-manager secret/environment facility, not an unprotected unit file or shared machine-wide environment.

### Manual foreground run

From the repository root, substitute the URL and client paths for the host. This example uses the explicitly opted-in private-LAN HTTP transport; omit the flag when using HTTPS:

```sh
python scripts/wake_daemon.py \
  --url http://192.168.50.90:8765 --allow-insecure-http \
  --agent codex --token-file "$HOME/.config/agent-bus/codex.token" \
  --state-dir "$HOME/.local/state/agent-bus-wake" \
  --executable "$(command -v codex)" \
  --cwd "/path/to/the/agent-workspace"
```

`--agent` must match the bearer-token identity and `--client`; the daemon rejects mismatches. `--cwd` is optional and selects the worker CLI working directory (for example, one where its MCP configuration applies). `--wait-timeout` is 1–60 seconds (default 60). `--run-timeout` is 1–3600 seconds (default 900). Stop with Ctrl-C/SIGTERM. The process lock and seen-ID database live in `--state-dir` and should stay on local user-owned storage, not a shared drive.

For the staging endpoint at `192.168.50.90:8765`, use HTTP only on the isolated, approved LAN and keep the firewall/allowlist narrow. No public tunnel, public DNS, or general VLAN exposure is implied.

## CLI modes used

The worker-specific command is constructed internally; untrusted message fields never become command arguments:

| Agent | Invocation shape | Permission behavior |
| --- | --- | --- |
| Hermes | `hermes chat --oneshot -q <fixed-prompt>` | Finite query; no resume flag. Tool availability/permissions remain in the Hermes profile. |
| Codex | `codex --ask-for-approval never exec --sandbox read-only --ephemeral <fixed-prompt>` | The approval policy is a global flag and must precede `exec`. Read-only shell sandbox; does not use `--full-auto`, `--yolo`, or the dangerous bypass flag. MCP permissions remain in Codex config. |
| Claude Code | `claude -p --permission-mode dontAsk <fixed-prompt>` | Print mode; denies rather than prompts for tools not already allowed. Do not use `bypassPermissions` or `--dangerously-skip-permissions`. |

Official references: [Hermes CLI commands](https://hermes-agent.nousresearch.com/docs/reference/cli-commands), [OpenAI Codex CLI reference](https://developers.openai.com/codex/cli/reference), [Claude Code CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-reference), and [Claude Code permissions](https://docs.anthropic.com/en/docs/claude-code/permissions). CLI upgrades may change flags; verify `--help` and the official reference before upgrading a service.

## Service examples

Run as the same unprivileged user that owns the token file and authenticated CLI profile. Configure only one daemon per agent; its lock is a second line of defense if a service manager is misconfigured. Do not run it as root, LocalSystem, or a shared machine account just to keep it alive.

### Linux: systemd user service

Create `~/.config/systemd/user/agent-bus-wake.service` and substitute absolute paths, endpoint, agent, and executable:

```ini
[Unit]
Description=Agent Message Bus wake daemon (Codex)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=simple
UMask=0077
WorkingDirectory=/path/to/the/agent-workspace
ExecStart=/usr/bin/python3 /path/to/internal-MCP/scripts/wake_daemon.py --url https://bus.example:8765 --agent codex --token-file %h/.config/agent-bus/codex.token --state-dir %h/.local/state/agent-bus-wake --executable /home/agent/.local/bin/codex
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

For the approved private-LAN HTTP endpoint, add `--allow-insecure-http` to `ExecStart`. Then install and inspect it:

```sh
systemctl --user daemon-reload
systemctl --user enable --now agent-bus-wake.service
systemctl --user status agent-bus-wake.service
journalctl --user -u agent-bus-wake.service
```

The unit rate-limits repeated fatal exits. The daemon itself already handles transient network errors; authentication/configuration errors require fixing the cause and restarting.

### macOS: LaunchAgent

Create `~/Library/LaunchAgents/com.example.agent-bus-wake.plist`, replacing the example paths and arguments. Each `ProgramArguments` item is a separate argv element—do not combine it into a shell command.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.example.agent-bus-wake</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/internal-MCP/scripts/wake_daemon.py</string>
    <string>--url</string><string>https://bus.example:8765</string>
    <string>--agent</string><string>hermes</string>
    <string>--token-file</string><string>/Users/agent/.config/agent-bus/hermes.token</string>
    <string>--state-dir</string><string>/Users/agent/.local/state/agent-bus-wake</string>
    <string>--executable</string><string>/Users/agent/.local/bin/hermes</string>
    <string>--cwd</string><string>/path/to/the/agent-workspace</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/Users/agent/Library/Logs/agent-bus-wake.log</string>
  <key>StandardErrorPath</key><string>/Users/agent/Library/Logs/agent-bus-wake.log</string>
</dict>
</plist>
```

Load at login with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.agent-bus-wake.plist`; inspect with `launchctl print gui/$(id -u)/com.example.agent-bus-wake`. It handles transient network outages internally. The example intentionally omits automatic restart on every fatal exit; fix invalid credentials/configuration and bootstrap it again.

### Windows: Task Scheduler

Use Task Scheduler to run the daemon as the same signed-in user that has the CLI installed and authenticated. The client may need that user's profile/MCP configuration; a `SYSTEM` task is not equivalent.

- **Trigger:** At log on for that user (or at startup only if the user profile and CLI credentials are available in that context).
- **Action → Program/script:** absolute path to `python.exe` (or the relevant virtual environment's `Scripts\python.exe`).
- **Add arguments:**

  ```text
  "C:\path\to\internal-MCP\scripts\wake_daemon.py" --url https://bus.example:8765 --agent codex --token-file "C:\Users\agent\AppData\Local\AgentBusWake\codex.token" --state-dir "C:\Users\agent\AppData\Local\AgentBusWake\state" --executable "C:\Users\agent\AppData\Local\Programs\Codex\codex.exe" --cwd "C:\path\to\agent-workspace"
  ```

- **Start in:** the chosen agent workspace.
- **General:** use the least-privileged task user; select “Run only when user is logged on” unless unattended access to the user profile and credentials has been verified.
- **Settings:** allow the task to run indefinitely; if already running, choose **Do not start a new instance**. Configure only a small bounded number of delayed restarts on failure (for example, three); do not repeat-start the daemon every five minutes because it is a continuous long-poll process.

For a private-LAN HTTP endpoint, add `--allow-insecure-http` to the arguments. Python and CLI paths with spaces must be quoted. Use Task Scheduler's History and the daemon's stderr/log destination for diagnostics; output from the AI CLI itself is suppressed.

## Tests

Run the offline standard-library test suite from the repository root:

```sh
python scripts/test_wake_daemon.py
```

It uses a local fake HTTP server and fake executable; it makes no LLM/API calls. It covers authenticated long-poll path/timeout, retries after a 503, UUID filtering and dedupe across restarts, one-process locking, file/environment token handling, argv/body injection isolation, and worker timeout. The bus wait route itself should also be exercised by the server/API integration tests before enabling a service.

## Operational limits and recovery

- The daemon does not claim messages. A pending message remains pending until the worker's configured MCP tool does so. If the worker lacks bus access, the daemon records one attempt and will not spin on it.
- A worker timeout or nonzero exit is logged with the UUID, exit status, and—when stderr matches a known category—a generic diagnostic label. Raw stdout/stderr, message bodies, and tokens are suppressed. Review the client CLI's own authenticated local history if diagnosis is needed; do not paste secrets or full message bodies into service logs.
- To re-arm an intentionally retried ID, stop the daemon and delete only its verified UUID row from the matching `seen-<agent>.sqlite3` `attempts` table (see above), then restart. Do not delete the whole state directory casually; doing so removes dedupe protection for all pending IDs.
- Stop/disable the service before changing its endpoint, identity, token file, CLI profile, or state directory. Do not deploy or enable a service as part of this source change.
