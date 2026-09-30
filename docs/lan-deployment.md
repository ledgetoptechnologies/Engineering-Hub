# Restricted LAN deployment

The tested phase binds Docker's published port to `127.0.0.1`. Do not change this until localhost tests, live MCP tests, restart durability, authentication, retention, and backup/restore checks pass.

For an approved LAN phase, bind the host port only to the selected Hermes interface and set `AGENT_BUS_ALLOWED_IPS=192.168.60.92,192.168.20.151`. The application checks `request.client.host` for both REST and MCP. Keep the host firewall restricted to those two source addresses and port 8765. Do not open the whole VLAN, expose TrueNAS, add Cloudflare, configure a tunnel, or publish DNS.

The compose file uses a container bind of `0.0.0.0` so Docker port publishing works; the host-side port mapping is the exposure boundary. Local `.env.example` intentionally publishes only to host loopback. A future LAN compose override should set the host-side published address explicitly after reviewing the actual interface.

Example review-only commands (do not apply firewall changes without separate approval):

```bash
ss -ltnp | grep ':8765'
curl -fsS http://127.0.0.1:8765/health
# From 192.168.60.92 and 192.168.20.151 only, test health and authenticated MCP.
```

Record the exact firewall command and output in the deployment change record. Plain HTTP must remain inside the trusted LAN; public exposure and TLS termination are out of scope for this local build.
