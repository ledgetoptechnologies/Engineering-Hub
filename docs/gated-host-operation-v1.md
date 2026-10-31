# Gated host operation enrollment

The v5 source path remains disabled by the installed executor's gate guard. This
document defines its explicit host input; it does not establish staging readiness,
exclusive generation writers, direct channel custody, or build provenance.

The host operator independently approves one candidate or rollback and places a
canonical UTF-8 JSON object in a root-owned mode-0600 regular file under a
root-owned mode-0700 parent. Protected ancestry and replacement checks follow
the supervisor's no-follow root-file policy. Its exact fields are:

- `schema`: `engineering-hub.host-gate-operation.v1`.
- `manifest`: the complete canonical pilot manifest object.
- `role`: `candidate` or `rollback`.
- `build_enrollment`: exact `manifest_digest`, `role`, `record_digest`,
  `builder_id`, and `invocation_id` independently approved by the operator.
- `record`: the complete canonical build-record object selected by that digest.
- `metadata`: an array of `{ "digest": "sha256:...", "base64": "..." }`
  objects containing every OCI metadata blob required by that record. Base64
  is standard padded canonical encoding. Blobs are bounded and hash checked.
- `application_enrollment_file` and `controller_key_file`: distinct absolute
  paths to the existing root-protected v4 application policy and controller key.

The loader checks the enrolled record and OCI metadata chain using the existing
build verifier. Root approval is the authority for the enrollment; labels or
hashes from an untrusted build are not independent approval. Preflight checks
the operation against the signed supervisor profile, exact committed runner
configuration, protected application policy and controller public key before
the supervisor creates an executor container. The operation file is rechecked
throughout the gate thread, including before start enrollment, ledger reserve,
decision issuance, consumption and final acceptance. A changed or unavailable
approval fails closed and preserves the original operation for inspection.

With all independent host prerequisites verified and the runtime guard later
deliberately enabled, the explicit operator invocation would be:

```text
python -I -m hub_container_executor.launcher_cli \
  --enrollment-file /protected/executor.json \
  --signing-key-file /protected/executor.key \
  --journal-file /protected/new-journal.sqlite3 \
  --held-ranges-file /protected/held-ranges.json \
  --gate-operation-file /protected/gate-operation.json \
  --gate-timeout-seconds 120 --wait-seconds 3600 --create-journal --execute
```

The gate thread serves one role and does not automatically retry uncertain
effects. The main supervisor continues serving issuer observations concurrently.
A stop requests gate cancellation and waits for the owned thread before closing
the supervisor; if it cannot join within the bounded wait, ownership remains
retained and the launcher reports failure rather than closing under live work.
An accepted ledger commit is authoritative; a lost reply is unknown to the
supervisor and requires separate authenticated lookup, not replay. Candidate
and rollback each need their own independently approved operation and fresh
one-shot supervisor/journal. Existing host isolation and held-engine restrictions
remain separate prerequisites.
