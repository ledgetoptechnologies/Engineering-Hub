# Inherited broker drain: source only

`serve_owned_inherited_broker` explicitly owns a preverified connected
`SOCK_SEQPACKET` descriptor and a fresh executor resource context. The installed
runner has a guarded inherited-channel composition, but the supervisor's paired
launch gate remains closed; this source does not enable paired launch.
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

## Factory failure and retained hold

In inherited mode the queued service durably reserves
`hub_pilot_remote_cleanup_hold` under its `PilotOwner` epoch **before** invoking
the trusted client factory. The factory is required not to issue commands, but
the service cannot prove what an arbitrary callable did before it raised or
returned an invalid object. Ownership of unaccepted factory resources stays with
that factory. A raise, invalid return or mismatched transport therefore leaves
the durable hold in place: there may be no accepted channel on which the service
can request and authenticate remote drain. Closing the local owner lock is not
evidence that the broker or a delegated holder has stopped. Later `PilotOwner`
acquisition refuses the held database. This is intentional, including when the
factory failure happened before the service itself submitted a command.

## Operator reconciliation: evidence only, no automatic release

There is currently **no supported recovery or hold-clearing operation** for a
later owner. The original owner can call `confirm_remote_cleanup` only after a
successful authenticated drain in its own live epoch. A lost acknowledgment,
failed factory, process crash or later operator observation does not recreate
that capability. Do not delete the hold row, edit the database, substitute a
new database or restore an older snapshot to make startup succeed.

Until a separately designed and reviewed recovery authority exists:

1. Stop new admission and coordinate the actual owners; retain the original
   database, lock, journal, enrolled configuration and exact process/launch
   identities. Do not stop or delete an unrelated process or resource.
2. Establish whether the factory transferred, retained or delegated a
   descriptor; record the original broker and executor identities and all
   accepted or uncertain operation IDs. A missing response is an unknown
   outcome, not proof that work was not performed.
3. Through the existing owner, drain accepted work where possible and verify
   the exact broker, executor and any holders are terminal. Preserve ambiguous
   work and its evidence. Do not infer drain from a closed local socket.
4. Preserve and inspect a consistent, independently verified copy of the
   coordinator and executor records only after their writers are quiescent,
   using the approved local SQLite procedure. Correlate the hold epoch with
   the original launch, operation journal and ownership evidence. A copied
   database or a matching Engine object alone does not establish exclusivity.
5. Keep admission closed and escalate the retained evidence to the authorized
   operator. Do not resume this database until a supported recovery mechanism
   and its exact authority are implemented, reviewed and separately applied.

## Version and storage boundary

The hold is enforced by code that knows this table. Older binaries can ignore
it, and a restored or replaced database can lose it. Deployment therefore
requires an independently enforced version and private-storage boundary that
prevents downgrade, alternate writers and rollback to an earlier snapshot while
the remote outcome is unresolved. Until that boundary is established, this
table must not be represented as a machine-wide or cross-version fence.
