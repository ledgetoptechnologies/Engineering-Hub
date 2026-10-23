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

This is **not yet a remote Hub observation path**. The installed executor still
needs an explicitly composed current-observation Unix client/HTTP relay, exact
authenticated route/private proxy allowlist and core Hub transport wiring.
Those are not implemented here. Future relay HTTP bearer authentication is
separate from signed evidence verification, with no generic trust header.
Independent release/current launch pins must arrive through trusted operator
handoff, never from accepting the response's claimed pins. Current identity
still does not prove inventory, writer exclusion, unknown-effects absence,
operator reconciliation or task execution approval. Tests use synthetic keys and
container validation with real UDS peer checks; they are not live staging proof.
