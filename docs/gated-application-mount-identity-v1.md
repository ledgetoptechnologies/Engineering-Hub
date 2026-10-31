# Proposed gated application image: mount identity v1

Status: **design, not an installed or enabled runtime**. This contract describes
work needed to verify the mounted state directory before an application can
perform effects. The disposable prototype measured an actual bind mount and
blocked a synthetic effect on mismatch; it did not establish this contract's
authority, isolation, recovery or delivery guarantees.

An explicit source-level hold inspection command is available for an installed
executor package: `python -m hub_container_executor.state_custodian_inspect --enrollment /absolute/root-private/inspection.json`.
The enrollment is a
root-owned mode-0600 canonical JSON file beneath a root-owned mode-0700 parent.
It contains `schema: 1`, absolute `journal_enrollment`, `journal_path`,
`owner_policy`, `copy_policy`, and `provider_key_path` paths, an independently
enrolled 32-byte `provider_public_key` as lowercase hex, and an `expected`
object with `target_id`, `operation_id`, `run_id`, `manifest_digest`,
`admission_digest`, `fencing_generation`, `device`, `inode`, `host_uid`,
`host_gid` and `owner_policy_digest`. These pins must be
obtained independently; do not copy unverified report values into the config.
The command uses the protected provider signing key and emits only verified
held metadata. It reports quiescence as unknown and cannot release the hold.
It opens the journal read-only, refuses SQLite sidecars or WAL mode, and must
obtain the journal's exclusive owner lock; it is unavailable while a cooperating
owner is active and is not a concurrent health check. Root, the host and the
protected enrollment remain trusted. This source command has not been deployed
or accepted on the staging host.

The current pilot requires the verified image's default entrypoint and command
(`container_pilot_manifest.py`, `daemon_backend.py`). Docker's create/inspect
response describes configured mount paths and flags; it does not prove the inode
eventually mounted. The existing executor and signed launcher profile remain
unchanged and must not accept a new gate by relaxing those checks.

## Proposed authority and image

1. Build a separately identified application image whose verified **default PID
   1** is a small gate. Its build record independently pins the runnable digest,
   platform manifest, image config, source revision, gate binary digest and the
   exact application executable and arguments. The gate starts no application
   child and performs no state write before authorization. It is not injected by
   overriding an existing approved image's entrypoint.
2. Introduce a new, explicitly versioned application launch policy and signed
   launcher enrollment. The policy binds the exact image release above, normal
   closed runtime limits, state mount target, one gate-control endpoint, and the
   controller trust key. The existing image-default policy and legacy releases
   retain their present behavior. The launcher remains the owner of Docker
   creation, process identity, journal, deadline, stop and drain; a container
   label, environment variable, or response from the candidate is not enrollment.
3. Independently enroll a controller and an exclusive generation custodian. The
   custodian opens the selected directory without following links, retains its
   descriptor, records its device/inode in the daemon's mount namespace, and
   prevents replacement or writes by other principals for the entire admitted
   lifetime. After release, the authorized application may write its assigned
   generation; all other writers remain excluded.
   This requires an enforced host boundary, not a cooperative pathname check.
   Root, daemon administrators and principals capable of replacing mounts or
   delegating descriptors are trusted unless a stronger boundary is verified.

## Proposed one-attempt exchange

Before any container create/start, persist a fresh launch intent bound to the
manifest digest, run and fencing generation, operation ID, immutable image
identity, selected state generation, expected directory device/inode, host boot
identity, deadline and independently enrolled controller key. Reserve a unique
one-shot challenge; never reuse it after a crash or uncertain delivery. The
expected directory identity is derived from the held custodian descriptor, not
from the candidate's response. The launcher checks the source identity and
custodian ownership immediately before starting and until the child is released.

The signed profile authorizes only a dedicated local control channel; it does not
authorize the application to contact the daemon or receive daemon credentials.
The gate opens the exact mounted target with `O_DIRECTORY | O_NOFOLLOW`, checks
`fstat` against the named target, and reports device/inode and mount flags over
that channel. Its bounded, canonical v1 message includes the protocol version,
challenge, launch/operation IDs, manifest digest, run/fence, image identity,
state generation, observed device/inode and a short expiry. The launcher binds
the channel to the enrolled live container/process and its start time; kernel
peer credentials alone do not prove exclusive possession or prevent descriptor
relay. The gate and launcher reject duplicate, reordered, truncated or unknown
fields and impose byte, time and message-count limits.

The controller issues one domain-separated authenticated decision over the same
bound exchange. It covers the entire observation, independent expected identity,
original challenge, launch/fence and expiry. The gate verifies the decision with
the independently pinned key, compares actual and expected identity, and retains
the opened directory descriptor through the transition. Only then may it `exec`
the exact separately enrolled application command. Retaining that descriptor
alone does not force the application to use it after `exec`: either enforce the
path and mount identity as immutable for the application's entire lifetime, or
use an application interface that actually operates through the retained
descriptor. A mismatch, unavailable
custodian, invalid identity, timeout, controller disconnect, process replacement,
clock regression or ambiguous decision fails closed without starting the child.
Expiry must use a monotonic ceiling as well as the signed wall-clock limit.

The durable journal records intent before start and the verified decision before
release. A lost response or crash after start is **unknown**, not proof that the
application did not execute. Hold the original operation and machine reservation;
do not issue another challenge, start a replacement, delete state, or retry an
effect until independently authorized reconciliation establishes the outcome.
An acknowledgement does not by itself prove later process liveness or continued
exclusive state custody.

The ledger's authenticated reservation and decision rows can detect modified
contents, but their MACs cannot detect restoration of an older, fully valid row
or an earlier complete database. A separate enforced owner and storage boundary
must prevent alternate writers, downgrade and rollback of the ledger and its
evidence key. A local transaction or MAC is not that boundary, and the source
does not enable the application gate or establish machine-wide exclusivity.

## Acceptance required before enabling

- Review the exact image, manifest/profile versions, key enrollment, command,
  control-channel custody and no-follow source/parent ownership as one unit.
  Prove the gate runs before application initialization and has no inherited
  writable daemon socket, unapproved network or other state mount.
- On the intended Linux daemon and user-namespace topology, measure the actual
  mounted device/inode and verify that its identity is comparable with the
  custodian's descriptor. Test nested mounts, symlinks, mount replacement,
  ancestor renames, differing namespaces and filesystem identity reuse. Reject
  unsupported or ambiguous comparisons; Docker Desktop results do not prove
  native staging behavior.
- With real kernel-backed isolated fixtures, show no application effect on
  wrong inode/device, wrong release, wrong launch/fence, swapped source,
  unexpected peer, repeated challenge, malformed message, expiry, disconnect
  and controller/gate crash at every exchange boundary. Show matching evidence
  permits exactly the enrolled command once. Exercise concurrent replacement
  attempts under the actual enforced custodian boundary, not only mock checks.
  Include replacement after a valid decision but before `exec`, and throughout
  the application's lifetime; an unprotected path or unrelated retained FD is
  not passing evidence.
- Show durable recovery and operator-visible held state after uncertain create,
  decision or exec; demonstrate no replay or release based solely on timeout.
  Exercise clean stop, accepted-work drain and resource cleanup under the real
  supervisor. Independently verify the existing machine-wide hold and outside
  writers before any live pilot.

These checks qualify only the particular enrolled image, daemon, filesystem and
host topology tested. They do not establish database correctness, application
health, rollback readiness, or authorization to release retained work.
