# Paired channel handoff: source-only boundary

`hub_container_executor.pair_handoff.serve_pair` is an explicit one-shot issuer
for two already measured `LaunchSupervisor` instances. Normal supervisor
construction still refuses the coordinator and inherited-channel profiles; this
module does not install a launcher, enable an executor, or establish host trust.

The caller supplies a fresh pair UUID and root-protected canonical enrollment and
signing-key files. The enrollment pins the pair public key, both exact launch IDs
and release digests, and the common manifest. The issuer independently checks
the protected file identities, both supervisor measurements and role identities,
creates the socketpair, and reserves both single-role issuance records. A fresh
root-private pair journal records both signed receipts before either descriptor
is sent. Its compare-and-swap transition to `committed` is durable before either
pair confirmation is sent. The recipients retain their descriptors until both
role signatures and the common signed confirmation verify.

The schema is `engineering-hub.pair-handoff-enrollment.v1`, with exactly
`schema`, `pair_id`, `pair_public_key` (lowercase Ed25519 public-key hex),
`manifest_digest`, `executor_launch_id`, `coordinator_launch_id`,
`executor_release_digest`, and `coordinator_release_digest`. A release digest is
the SHA-256 of the canonical full release enrollment, with its public key in
lowercase hex and platform as its closed JSON object. These records are not
provisioned by this module. The pair signing key is exactly 32 raw bytes in a
separate root-owned private file.

Final confirmation delivery is not atomic. If one send succeeds and the other
fails, one recipient may already have a descriptor. The pair is held and the
original journal is retained; a commit does not prove both recipients received
or retained their descriptors. An ambiguous journal commit also prohibits retry.
No automatic adoption, rollback, cleanup or reissue is provided.

`SO_PEERCRED` identifies the original stream connector, not a later holder of a
delegated stream. Direct non-relayed receipt, isolated identities and descriptor
custody, actual kernel namespace mappings, writer exclusion, and independently
enrolled policy and host state remain separate operational prerequisites. The
signed receipts do not establish opposite-end custody or ongoing process health.
The synthetic tests do not establish those properties or a live staging pilot.
