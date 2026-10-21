# Source-only executor proxy renderer draft

`scripts/render_executor_proxy_bundle.py` exposes `render_proxy_bundle(...)`,
which returns six in-memory files:
complete NGINX configuration, manual systemd unit, private NSS files, operator
preflight/drain notes and public content manifest. It does not read host state,
write files, publish a unit, install packages or start anything.

Required explicit inputs are a new canonical v4 generation UUID, its exact
`/var/lib/eh-pilot-volume-test-v1/campaigns/hub-pilot-cli-<UUID>/executor-state/api`
parent, `executor.sock`, bind `192.168.50.90`, approved source `192.168.50.80`
and measured expected worker UID210000. No other listener/upstream/account/path
policy is accepted. Known 5550/102ca430/0af sessions are rejected. Other proposed
UUIDs still need independent live freshness/ownership approval.

The existing repository HTTP template is embedded unchanged apart from replacing
its six explicit placeholders. Outputs preserve caller bearer, exact route maps,
single UDS upstream, no retry and original timeout/buffering semantics. The unit
uses only a read-only API-parent alias, NGINX binary/config/NSS aliases and two
existing server TLS credentials. It receives no Engine socket, executor key,
database or bearer. Host network access is explicit; this is not a privateNET
fixture or a claim of network-layer isolation.

Do not mistake rendering for readiness. The generated `OPERATOR.md` requires
live generation/receipt/PID/inode/permission/resource validation, actual sandbox
mount verification and actual NAS-network-context TLS acceptance. V5 established
binary alias/TLS-material behavior, not the new API-parent mount or this unit.
In particular, the `/var/lib` hiding and `/run` read-only aliases must be checked
in the actual composed sandbox before admission, not weakened if they fail.

No boot installation, timer, restart loop or automatic lifecycle operation is
generated. Operator-owned bounded lifetime and deliberate drain/own-master QUIT
are mandatory. A filesystem denial does not protect worker-inherited key memory.

## Inert CLI

From the repository root, pass an independently approved fresh generation and
its exact derived API parent. This example UUID is proposed data, not enrollment:

```sh
python scripts/render_executor_proxy_bundle.py \
  --generation-id 1d2a891d-0ea8-4ec0-8f88-4984b397fc8a \
  --api-parent /var/lib/eh-pilot-volume-test-v1/campaigns/hub-pilot-cli-1d2a891d-0ea8-4ec0-8f88-4984b397fc8a/executor-state/api
```

Stdout is one JSON object with `status: rendered_not_activated` and the six
proposed file texts (including their public content manifest). The command
does not save files or contact any computer. Listener, NAS source, worker UID,
socket leaf, TLS inputs and sandbox profile are fixed, not generic CLI controls.

This artifact is for a bounded manually owned generation. It does not implement
the permanent owner, durable admission/drain, receipt renewal, restart recovery
or authoritative Hub enrollment needed for a long-running first release.
Rendering, tests and Type=exec establish neither actual NAS connectivity nor
runtime readiness or reboot acceptance.

Tests are pure generation/negative-input/property checks, not NGINX/systemd or
real NAS-to-staging runtime proof. Exact systemd kill behavior was checked against
[systemd.kill](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html);
NGINX's graceful master signal is documented in
[Controlling nginx](https://nginx.org/en/docs/control.html).
