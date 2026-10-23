# Owned supervisor observation serving (source only)

The existing single-launch `LaunchSupervisor` now has a trusted synchronous
`observe_current_launch(nonce=..., timeout_ms=...)` API. It signs only after the
original one-shot receipt was successfully sent and durably recorded. It reuses
the SAME retained signer, release enrollment, Engine/socket/namespace authority,
consumer pidfd/start/boot/container and effective profile verification. No
observation creates, starts, stops, removes, adopts or renews a container.

The original wall approval ceiling is captured once beside the original local
monotonic launch ceiling, before the durable create reservation. The wall
ceiling is retained in the same root-private launch journal, including later
state records; Engine ID/PID/start are retained with launch identity there.
Historical journal rows are evidence, not permission to adopt or resume a launch.
The signed expiry is clipped to both original ceilings and at most five seconds
from this observation. Observations never recompute a renewed launch approval.

After its unchanged one-shot receipt exchange, `launcher_cli` calls
`wait_for_stop_with_observations(timeout_ms=..., stop_requested=...)`. This is a
bounded select loop in the SAME explicitly started launcher process, using its
already retained Unix listener and consumer pidfd. There is no daemon, second
listener, automatic service, thread, credential or activation flag. `--wait-seconds`
still bounds only this post-receipt period. Invalid requests do not reset it.
Approval expiry disables observation serving, without stopping the container.
Only an explicit operator stop signal invokes the existing graceful-stop path;
wait expiry or uncertainty preserves evidence and closes descriptors only.

`serve_one_current_observation(timeout_ms=...)` requires the exact original
executor init PID/UID/GID from Linux `SO_PEERCRED`. Another same-UID process cannot
relay as the approved init process. A request is one four-byte big-endian length
prefix plus at most 256 canonical JSON bytes, then SHUT_WR/EOF. Exact fields are
`protocol: engineering-hub.executor-current-observation-request.v1` and `nonce`
(64 lowercase hexadecimal characters). No PID, container, release or operator
selectors are accepted. The response uses the separate core observation
protocol/audience/Ed25519 signature domain documented in
[current-executor-observation.md](current-executor-observation.md), not the
one-shot receipt's request protocol or signing domain.

Malformed input, wrong peers, EOF/size/timeout/transport failures close that peer
and return a rejected result, without holding an otherwise healthy launch.
Actual retained identity/authority failures hold it and retain original journal
evidence. A disconnected observation requester is not evidence that a container
must be stopped. The whole per-request budget includes lock admission, frame
read, validation, signing and response send; the overall owned wait is not renewed.
Native filesystem/Engine checks remain cooperatively bounded, not forcibly
interruptible. The issuer does not cache or adopt a launch receipt as current
evidence, and its original one-shot receipt behavior is unchanged.

The installed executor composes a separate `UnixCurrentObservationTransport`
after its original launch receipt has passed verification. It reuses the same
explicit issuer socket path and UID/GID. No configuration, credential, port or
service is added. The new transport supports independent no-retry exchanges,
using the request protocol above; it does not expose `issue_receipt` or cache
signed results. Whole-call remaining budgets may be 1..10000 milliseconds,
because body/dispatch time must be deducted from the configured 100..10000ms
HTTP operation budget rather than minting a fresh minimum window.

An explicitly supplied observation transport enables only authenticated
`POST /v1/container-pilot/current-observation` on the existing executor listener.
The request has exactly one `nonce` field and at most 256 bytes. The relay uses
the existing dedicated executor bearer, rejects malformed/duplicate framing,
queries and other methods, and returns unchanged canonical signed-envelope
bytes with explicit Content-Length, application/json, no-store and Connection:
close. No PID, issuer, launch, release, key or deadline selector is accepted.
The inert private NGINX template admits that exact additional method/original
URI while retaining its original NAS restriction, TLS and executor UDS.

Observation and command work have separate no-queue admission gates. A busy
command therefore does not falsely invalidate current identity; simultaneous
observation requests are denied, not queued. Accepted raw executor Futures drain
through repeated cancellation before their gate is released. The service drains
HTTP and adapter/provider work before the installed runner closes its borrowed
observation transport. The synchronous transport enforces remaining socket I/O
deadlines; native filesystem work remains cooperatively bounded. Neither this
relay nor a successful HTTP bearer check verifies signed identity claims.

This is **not yet installed NAS-to-staging acceptance**. The core HTTPS transport
has synthetic local TLS and signed round-trip tests; trusted live composition
still requires separate verification. HTTP bearer authentication is separate
from signed evidence verification, with no generic trust header.
Independent release/current launch pins must arrive through trusted operator
handoff, never from accepting the response's claimed pins. Current identity
still does not prove inventory, writer exclusion, unknown-effects absence,
operator reconciliation or task execution approval. Tests use synthetic keys and
container validation with real UDS peer checks; they are not live staging proof.
