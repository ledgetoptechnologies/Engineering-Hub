# Restricted LAN deployment

The local test service is running on the Hermes host at `192.168.60.92:8765`.

Docker uses `network_mode: host` so the application sees the real client IP. The service binds to `0.0.0.0` at the host level, but the application allowlist is:

```text
127.0.0.1,192.168.20.151
```

The host firewall allows TCP/8765 only from `192.168.20.151` (Beau's main rig/Codex). Local Hermes checks use `127.0.0.1`. Bearer authentication is still required for all REST and MCP operations, including health-independent message operations.

No public DNS, Cloudflare exposure, tunnel, TrueNAS deployment, or whole-VLAN firewall rule is configured.

## Codex connection

MCP endpoint:

```text
http://192.168.60.92:8765/mcp
```

Health endpoint:

```text
http://192.168.60.92:8765/health
```

Codex must use its separate `CODEX_TOKEN` as an HTTP bearer token. The token must be transferred through a secure local mechanism and must not be committed, pasted into GitHub, or placed in command-line history.

## Verification commands

From the Hermes host:

```bash
ss -ltnp | grep ':8765'
curl -fsS http://127.0.0.1:8765/health
```

Expected: a listener on `0.0.0.0:8765` and health JSON with `"ok":true`.

From Beau's main rig, verify TCP reachability and authenticated MCP. An unauthenticated request must fail; a request using the Codex token must initialize and list exactly these tools:

```text
send_message
receive_messages
claim_message
ack_message
reject_message
get_message
```

If Codex cannot connect, check the Hermes-host UFW rule, the main-rig source IP, and that the service remains bound to `0.0.0.0:8765`. Do not open the port to an entire VLAN without explicit approval.

Plain HTTP is acceptable only inside the trusted LAN. Public exposure and TLS termination remain out of scope for this testing phase.
