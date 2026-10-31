# Inherited broker drain: source only

`serve_owned_inherited_broker` explicitly owns a preverified connected
`SOCK_SEQPACKET` descriptor and a fresh executor resource context. No installed
runner currently selects it, and this source does not enable paired launch.
The caller must first establish the independently enrolled pair, descriptor
custody, peer account mappings, and host isolation.

The inherited channel accepts a sequenced `drain` request only when explicitly
enabled by this service. It is checked against the same kernel per-packet sender
account as other commands. The service closes admission, finishes accepted work,
closes the adapter and exits the resource context before sending a drain
acknowledgment. A timeout, peer loss or cleanup failure closes the channel without
an acknowledgment; the outcome is uncertain and must not trigger a replay.
The caller supplies a fresh `BrokerCleanupReport` and must inspect its bounded
state and code after completion or cancellation. The report does not authorize
work; it preserves an unverified-cleanup diagnostic without exposing exception
details when cancellation remains the caller-visible outcome.

Authentication here establishes the expected account, not a particular process:
a same-account holder of a delegated descriptor can send packets. Host policy
must prevent unintended descriptor delegation and privilege-based credential
spoofing. A local stop or cancellation also closes admission and waits for an
accepted call, but cannot forcibly interrupt synchronous backend work. The
existing supervisor and installed runner remain responsible for process identity,
signal handling and their own bounded shutdown; this source alone does not prove
either peer is ready or that a paired pilot is active.
