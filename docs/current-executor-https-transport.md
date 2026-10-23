# Current executor HTTPS transport

`CurrentExecutorHTTPSTransport` is a core-only synchronous transport implementing
`observe_current_launch(*, nonce, timeout_ms) -> bytes` for
`CurrentExecutorObserver`. It is not identity authority, a launch receipt, an
inventory reconciler, a session opener or permission to run a job.

The trusted composition must independently supply an HTTPS literal-IP origin,
protected CA and dedicated executor credential files, and their retained SHA-256
pins. The existing Linux protected-file reader captures stable, no-follow bytes;
the credential requires current-UID ownership, mode 0600 and a private parent.
The CA is loaded from captured bytes, not reopened by TLS. No default CAs,
environment proxies, key logging, hostname discovery or optional executor package
are consulted. TLS validates both the explicit CA and the literal IP SAN.
Scoped IPv6 addresses are rejected. This class cannot determine whether supplied
credential bytes were reused from an agent: the enroller must prohibit that.

Trusted installed compositions that have already captured protected bytes may
instead use `CurrentExecutorHTTPSTransport.from_captured_material(origin,
ca_pem=..., ca_sha256=..., credential=..., credential_sha256=...)`. It performs
the same exact origin, pin and TLS checks without file/network I/O, discovery or
reopening paths. CA and credential inputs must be immutable `bytes`, respectively
1–1048576 and 1–4096 bytes; credentials contain printable non-space ASCII only.
This constructor does not establish how supplied material was captured or that
it is independent of agent tokens. That authority stays with the trusted caller.
Both constructors share one validator; the file constructor still rejects bad
origins/pin formats before reading protected files.

Each call uses a new socket, sends exactly one request and closes it. Independent
calls share no connection pool, retry state or locks; the identity observer owns
its own serialization and sticky denial policy. A transport failure alone does
not stop the executor. The deadline starts once per call and is carried through
connect, TLS handshake, send, every underlying read, parsing and final validation.
No event loop or background worker is created. OS scheduling and synchronous
cryptographic work are not forcibly preemptible, but late success is rejected.
The observer's configured exchange timeout is 100–10000 ms; the transport accepts
the remaining internal budget of 1–10000 ms after observer admission/validation.
It never rounds a nearly exhausted budget up to a new 100 ms window.

Wire contract:

- `POST /v1/container-pilot/current-observation` with bearer authentication and
  canonical JSON body `{"nonce":"<64 lowercase hex characters>"}` only.
- `Connection: close`; no redirect or automatic retry.
- Exactly status 200, `Content-Type: application/json`, one canonical positive
  decimal Content-Length, at most 8192 bytes. Transfer-Encoding and
  Content-Encoding are rejected, as are duplicate headers, obsolete folded or
  malformed fields, more than 64 status/header lines or 8192 total header bytes.
- The exact declared body plus EOF must arrive within the same deadline. Truncated
  bodies, trailing bytes, noncanonical/duplicate-key JSON and envelope keys other
  than `payload` and `signature` are denied. Payload identity and signature
  authority remain the separate observer's job, using independently retained
  issuer, release and launch pins.

The EOF requirement deliberately denies a peer that ignores `Connection: close`.
It prevents hidden extra body bytes after Content-Length rather than accepting a
pooled response. This assumes the narrow proxy/backend route honors close.

Tests use local TLS with synthetic certificates and signed fixture envelopes,
including wrong CA/IP SAN, slow handshake/header/body/EOF and framing failures.
They do not prove a NAS route, proxy installation, real executor identity,
operator enrollment, deployment, recovery or reboot acceptance. Nothing in this
module automatically wires or activates a service or changes configuration.
