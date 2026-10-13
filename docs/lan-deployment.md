# LAN service endpoints (reported status only)

The operator reports a staging Agent Message Bus at `192.168.50.90:8765` healthy for ten days. This task did not query it, and that host does not contain this repository checkout. Treat the staging status as operator-reported, not as verification of the image or Compose file in this checkout.

The TrueNAS deployment sample is a separate target at `192.168.50.80:8765`. It is not deployed by this work. See [`truenas-deployment.md`](truenas-deployment.md) for the private GHCR package, local ZFS storage, host binding, application allowlist, and health-check requirements.

The sample's intended client IPs are Hermes `192.168.60.92` and the Windows Codex/Claude machine `192.168.20.151`. Confirm current addresses and restrict the host/router firewall to those sources before use. There is no public DNS, Cloudflare route, tunnel, or whole-VLAN rule in this scope. The endpoint uses plain HTTP and bearer tokens, so keep it on a trusted, tightly restricted LAN.

MCP tools are available only while a client process is connected; this endpoint does not itself wake or schedule agents. See [`three-agent-integration.md`](three-agent-integration.md) for client setup and explicit shared-knowledge boundaries.
