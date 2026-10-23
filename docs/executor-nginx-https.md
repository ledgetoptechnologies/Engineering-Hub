# Optional executor HTTPS boundary

This is an inert configuration template, not an installed listener or an
approved host pilot. The intended route is:

`NAS unified Hub → verified HTTPS → dedicated standard NGINX → private executor UDS`

Use [the template](../docker/nginx-executor-https.conf.template) in a dedicated
NGINX `http` context, outside the executor's rootless, network-none container.
Do not attach NGINX to the production Hub, Cloudflare tunnel, Ops or Viewer.
No custom proxy runtime is needed. Ordinary Hub startup stays unchanged.

## Required operator evidence before activation

- Select the staging bind IP, executor DNS name, certificate/key paths and exact
  existing executor UDS path; replace every placeholder. The certificate must
  cover that DNS name (or IP SAN when explicitly using an IP). Pin its CA bytes
  in the NAS pilot profile. Do not use disabled certificate verification.
- Confirm the actual source address NGINX will observe from the NAS, then replace
  `__APPROVED_NAS_IP__` with that single address. The expected NAS LAN address is
  `192.168.50.80`, but routing/NAT must be measured, not guessed. Do not broaden
  the allowlist or add forwarded-header source rewriting to make a test pass.
- Record the actual NGINX worker UID/GID, host-to-rootless UID mapping, socket
  owner/mode and traversal permissions of every parent. Prove that this worker
  can reach the enrolled UDS without granting general socket access. Current
  source expects a private executor socket; no `chmod 666`, generic Docker socket,
  group-wide privilege or fabricated mapping is acceptable. If the current
  private topology cannot support that worker, stop for explicit design approval.
- Keep the TLS private key outside executor/agent-writable content. Provisioning
  certificates, identity mappings, firewall/service rules or socket mounts is
  separate authorized work, not an effect of this template.
- Use a dedicated, reviewed NGINX configuration without inherited authentication,
  `real_ip`, rewrite, additional location, retry, cache or debug-log behavior.
  Confirm actual rendered configuration with `nginx -t` and inspect `nginx -T`
  locally. Do not paste private key/token contents or configuration secrets.

## Wire boundary

Only these exact methods and original URI forms are forwarded:

| Method | URI | Body |
| --- | --- | --- |
| POST | `/v1/container-pilot/commands` | Existing closed request JSON, ≤8192 bytes |
| POST | `/v1/container-pilot/current-observation` | Exact nonce-only JSON, ≤256 bytes; unchanged signed envelope ≤8192 bytes |
| GET | `/v1/container-pilot/commands/<canonical-lowercase-UUID>` | Empty |
| POST | `/v1/container-pilot/commands/<canonical-lowercase-UUID>/reconcile` | Empty |

Query strings, aliases, encoded path spellings, trailing slashes and other
methods/routes are rejected. The executor still validates bearer identity,
request framing, UUID, manifest, fencing and policy; proxy admission grants none
of those. Authorization is passed from the caller, never supplied by NGINX.
NGINX may reject duplicate Authorization headers itself; the requirement is
that they never authorize an executor operation, not that they reach the app.

The template uses NGINX's original-request variable and case-sensitive map
expressions to avoid normalized-path aliases. Its source allowlist examines
the connection peer, not claimed forwarding headers.
([map](https://nginx.org/en/docs/http/ngx_http_map_module.html),
[request variables](https://nginx.org/en/docs/http/ngx_http_core_module.html#variables),
[access](https://nginx.org/en/docs/http/ngx_http_access_module.html))

The upstream is one explicit Unix socket, with no URI replacement, retries,
cache or alternate endpoint. Headers and body are forwarded; buffering and
timeouts are explicit. Read/send timeouts are inactivity limits, not a total
operation deadline; the Hub client retains its bounded whole-operation deadline.
No response-size security guarantee is inferred from buffer sizes: the executor
and Hub client retain their existing 16KiB response limits. If a connection is
lost after submission, inspect the original command; never retry it under a new
identity. Keep graceful service drain long enough for accepted work; do not add
a short force-kill shutdown deadline.
([proxy directives](https://nginx.org/en/docs/http/ngx_http_proxy_module.html))

TLS1.2/1.3 are explicitly selected. The NAS client must verify both the pinned
CA chain and configured hostname; a successful NGINX configuration check does
not establish either.
([TLS directives](https://nginx.org/en/docs/http/ngx_http_ssl_module.html#ssl_protocols))

## Acceptance and rollback

Before a pilot, prove correct/wrong CA and hostname, approved/other source IP,
missing/wrong/duplicate Authorization, the three routes and denied variants,
oversized/framing errors, preserved command identity, and original-command
inspection following an ambiguous disconnect. Observe actual UDS permissions,
one upstream attempt, successful health/rollback and drain before restart.

The optional `tests/test_hub_executor_nginx_wire.py` smoke exercised real
`nginx -t` and verified TLS to the actual executor ASGI application over a
disposable Unix socket. The local validation used official Docker Library
NGINX1.28.0, immutable image digest
`sha256:552e7481ca93ffccd046aa658dbbed22caefbc09c66fa7cd247cbb90b8a5c609`.
Its 23 passing tests (21 static contracts and two wire tests) covered the three
routes, wrong CA and wrong hostname rejected before any upstream request,
denied methods/aliases/query paths, source allow/deny despite spoofed
forwarding headers, original bearer preservation, absent/wrong/duplicate bearer
denial, oversized/ambiguous framing, bodyless inspection/reconcile, and one
upstream attempt on503. The test container ran UID568, network-none, read-only,
with no capabilities, host mounts or published ports; certificates and keys
were synthetic and disposable. Without NGINX, the optional wire tests skip.

This is not installed-host permission, firewall, real TLS deployment, NAS
integration or processing acceptance. Rollback is removal of this dedicated
optional listener configuration after draining it, without changing existing
applications, endpoint tokens, state or executor receipts.
