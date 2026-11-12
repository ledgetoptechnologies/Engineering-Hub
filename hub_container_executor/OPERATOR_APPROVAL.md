# Explicit operator approval candidate

This is an opt-in candidate, not an installer or an authorization to launch.
A separately authorized operator must independently review the launch before
using a pre-provisioned approver key distinct from the supervisor issuer key.
The root-private policy and key must be existing mode 0600 files under trusted
root-owned mode 0700 directories. The policy directory and the delivery
directory must be on the same filesystem. The delivery directory is separately
provisioned mode 0700 with the configured runner host UID and GID; the runner
must see that directory through its independently configured read-only mount.

A root-private JSON policy is a closed object with schema
`engineering-hub.operator-approval-policy.v1` and these fields:

- `release`: the independently enrolled `CurrentExecutorReleasePins` fields;
  `public_key` is lowercase Ed25519 public-key hex and `platform` is the
  enrolled platform object.
- `expected_engine_id`, `approver_public_key` (32-byte lowercase hex),
  `receipt_id`, `receipt_sha256`, and `launch_sha256` identify the independently
  reviewed proposal. The launch hash is SHA-256 of the UTF-8 canonical JSON of
  the full verified `CurrentLaunchPins` object, with sorted keys and compact
  separators. The receipt hash is the proposal's signed `receipt_sha256`.
- `receipt_expires_at_ms` and `approval_expires_at_ms` are independently
  reviewed integer ceilings, in Unix milliseconds. The signed record is clipped
  to both and to the original signed launch ceiling; the runner additionally
  checks the authenticated receipt's own expiration.
- `output_path`, `output_uid`, and `output_gid` select the new approval file and
  the independently provisioned runner directory owner. IDs must be nonzero.

Review requires a trusted method of establishing these inputs; a signed proposal
alone is not independent operator approval. Verify its issuer and fresh nested
observation, compare its complete launch identity to independent host evidence,
and provision the policy only after that review. Request a fresh proposal after
review and before publishing: its nested observation is valid for at most five
seconds. The signer re-verifies freshness and exact reviewed pins immediately
before publication. It cannot prevent revocation or expiration between its last
check and the atomic filesystem operation.

Run `python -m hub_container_executor.operator_approval --policy /root/private/policy.json --proposal /root/private/proposal.json --signing-key /root/private/approver.key --execute`.
The output is atomically moved into the runner directory without replacing an
existing name. Failure after staging may leave a root-private temporary file for
inspection. No key, directory, enrollment, policy, or approval is created by a
dry invocation without `--execute`. This does not establish installed-runner,
read-only-mount, Docker, staging, or production acceptance.
