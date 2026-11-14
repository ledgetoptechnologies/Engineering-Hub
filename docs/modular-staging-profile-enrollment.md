# Modular staging profile enrollment: source prerequisite

This prerequisite records an owner-selected, nonsecret profile and makes
its identity available to project-authorized readers. It does not change worker
configuration, attest that a worker uses the profile, modify a task review,
dispatch a job, clear a hold, or grant execution or release authority. Existing
unbound tasks and jobs retain their current behavior; this change is not a
complete solution for profile-bound staging admission.

There is no separate profile-enrollment feature switch. The routes are
registered with the staging API and become usable when the existing modular
staging controller is configured; its resource-store preparation creates the
additive enrollment table. A database restore can lose enrollments and audit
entries written after the backup, and older software does not enforce them.

`modular-staging-profile-v1` uses the exact closed fields in
`engineering_hub.modular_staging_profile.FIELDS`, an explicit integer CPU quota
in thousandths and explicit workspace size. Its bytes are sorted, compact,
ASCII-escaped JSON; the SHA-256 is domain-separated by
`engineering-hub:modular-staging-profile:v1` followed by a NUL byte. Only
nonsecret recipe values belong here; argv is visible to project readers. The
`from_worker_recipe` converter accepts the worker's **effective normalized**
recipe after its own defaulting, rejects all extra fields and refuses CPU values
that do not map exactly to whole thousandths. This module is not yet used by the
worker, nor does it prove that an existing worker's config is protected.

An authenticated configured human administrator may `PUT` the project/recipe
profile with the exact current `expected_generation` (zero for first enrollment).
Each successful write appends an immutable generation and an audit entry in the
same transaction; it does not publish an unscoped agent event. Stale writers get
a conflict and do not replace the current generation. The response explicitly
states `execution_authorization: false`. A configured administrator or actor
with that project's explicit staging `read` permission may `GET` the latest or
one historical generation. Project archival and grants are still enforced.
Enrollment does not mean review or consent to run a particular source commit.

Future work must bind an independent task review to the exact commit and enrolled
profile generation/digest, persist that tuple through queue/worker admission and
original receipts, reject stale or mismatched config before effects, and retain
legacy unbound held work without replay. Identity, isolation and host fencing are
separate: a worker's self-reported digest alone is not proof of its runtime or
exclusion of outside writers. No live profile or policy is created by this code.
