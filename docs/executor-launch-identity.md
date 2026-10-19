# Bounded executor launch-receipt verification

Status: explicit source implementation; no issuer, IPC service or trusted
installation has been provisioned. This is not real-pilot or reboot acceptance.

`hub_container_executor.runtime_identity.LaunchReceiptIdentityVerifier` implements
the host adapter's `implementation_commit()` interface by consuming one signed
launch receipt. It does not obtain identity from the pilot manifest, Docker
backend, image labels, package metadata or mutable checkout hashes.

## Independent enrollment and composition

Trusted operator composition supplies an `ExecutorReleaseEnrollment` containing:

- `issuer_id` and the independently pinned 32-byte Ed25519 public key;
- approved `implementation_commit`;
- `runnable_digest`, `platform_manifest_digest` and `image_config_id`, retaining
  their distinct OCI meanings;
- the shared `Platform` and independently approved `launch_profile_digest`.

Construction validates and snapshots these inputs, not their approval. Do not
derive enrollment from the receipt being verified or a task's expected identity.
Private signing keys belong only to the independent issuer; this module generates
no signing keys and accepts no request-selected key or algorithm.

The verifier requires explicit `max_receipt_lifetime_seconds` (integer 1–3600),
`exchange_timeout_ms` (integer 100–10000) and a caller-owned synchronous
`issuer_transport.issue_receipt(nonce=..., timeout_ms=...)`. It generates a fresh
internal 64-character lowercase hexadecimal nonce and calls that transport once.
There is no retry, refresh, automatic service discovery or default transport.
The resource factory does not construct or approve this verifier implicitly.

The transport **must exchange directly from this consumer process** with the
independently trusted issuer. Relaying a nonce through another permitted process
does not prove this consumer's identity. The actual issuer must authenticate its
kernel peer and correlate that peer—not a caller-supplied victim PID/container—to
the live immutable executor, host boot, process start and effective launch profile.
The receipt consumer cannot establish that kernel mapping from signed fields.

## Fixed receipt format

The transport returns at most 8192 bytes of canonical UTF-8 JSON, without floats,
non-finite values, duplicate keys or extra fields. Canonical encoding uses sorted
keys, compact separators and unescaped Unicode, as implemented by the module.
The exact envelope contains `payload` and `signature` only. `signature` is the
unpadded canonical URL-safe Base64 encoding of a 64-byte Ed25519 signature over:

```text
b"engineering-hub-launch-receipt-v1\x00" + canonical_json(payload)
```

There is no algorithm negotiation. The payload has exactly these fields:

| Fields | Required binding |
| --- | --- |
| `protocol` | `engineering-hub.executor-launch.v1` |
| `audience` | `engineering-hub.container-executor` |
| `nonce` | Exact internally generated challenge |
| `receipt_id`, `launch_id`, `boot_id` | Canonical UUID strings |
| `issuer_id`, `implementation_commit` | Exact independent enrollment |
| `runnable_digest`, `platform_manifest_digest`, `image_config_id` | Exact independent enrollment |
| `platform`, `launch_profile_digest` | Exact independent enrollment |
| `authenticated_host_pid`, `process_start_ticks` | Positive signed integers, at most 2^63−1; issuer-authenticated identities, not local PID comparisons |
| `container_id` | Exactly 64 lowercase hexadecimal characters |
| `issued_at_ms`, `expires_at_ms` | Positive integers at most 2^63−1, subject to freshness/lifetime checks |

The issuer's host PID may differ from the consumer's PID in a container namespace.
Do not compare their numbers as a substitute for authenticating the peer.

## Time, process and cleanup semantics

Issuance permits at most the fixed five-second wall-clock skew around the exchange;
the signed interval must be positive, within the explicit lifetime ceiling, and
unexpired at receipt. The accepted deadline is capped by both the signed remaining
interval and the lifetime measured from **before** the exchange. A slow response
does not restart the TTL. Parsing and signature verification count toward the
exchange budget. Subsequent checks use a fixed monotonic deadline, not renewed
wall-clock readings.

The synchronous exchange timeout is cooperative. Checks reject an overrun but
cannot interrupt a blocked transport or operating-system call. An async transport
is rejected; an incorrectly returned unstarted native coroutine is closed without
executing it. Foreign Futures/started coroutines are not cancelled or adopted.

After acceptance, `implementation_commit()` performs no issuer or Docker I/O.
It returns the independently enrolled commit only in the constructing process,
before monotonic expiry and before `close()`. The PID check precedes its lock so
a forked child cannot block on an inherited locked mutex. `close()` revokes only
this verifier and does not close the caller's transport. The caller owns that
transport and the verifier's lifecycle. Expiry/revocation prevents future identity
checks; neither cancels already-admitted container effects or releases reservations.

These receipts describe independently attested **launch conditions for a bounded
interval**, not memory attestation or proof of continuing runtime integrity.
An in-process signature check cannot make a malicious interpreter trustworthy.

## Required real installation acceptance

The missing issuer/transport must be independently reviewed and exercised against
the actual executor packaging, immutable image chain, effective command/mounts,
read-only code, host boot/process mapping and authorization boundary. Do not use
the backend's own report or a synthetic signer as that authority. The current Hub
image does not install the root-level executor package; the validation image does.
The source distribution now includes executor source, but that is not an installed
executor runtime or a deployment/activation command.

Only after independent enrollment, real peer mapping and the remaining host/writer
checks pass may the [pilot preparation procedure](staging-pilot-preparation-2026-10-19.md)
be presented for activation approval. Existing MCP endpoints, credentials and
production services remain separate and unchanged.

Implementation references: [Cryptography's Ed25519 API](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ed25519/)
and [Python isolated-mode options](https://docs.python.org/3/using/cmdline.html#cmdoption-I).
Isolated Python startup alone is not runtime attestation or an independently
approved launcher; global site startup and the installed dependency boundary must
also be controlled.
