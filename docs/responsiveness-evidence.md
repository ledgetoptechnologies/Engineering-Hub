# Responsiveness evidence

Both main-branch verification jobs upload structured responsiveness diagnostics
after pytest, even if tests fail. Artifact names include the source SHA, workflow
run and attempt; an unchanged rerun never replaces the earlier attempt's result.
Retention is 14 days. The core Hub release evidence is a separate artifact.

`EH_RESPONSIVENESS_ARTIFACT_DIR` opts the isolated test helper into capture. Each
invocation writes a unique JSON file, preserving a complete known-schema report
up to 1 MiB, including samples, maximum, strict limit, source hashes, cleanup and
negative-control facts. Unknown fields and unverified source paths are omitted
and counted. Arbitrary stdout/stderr and environment contents are never saved.
Malformed, duplicate, oversized or nonfinite reports retain only bounded failure
metadata. Timeout evidence records the error class, not captured child output.

Artifacts are diagnostics, not an alternative pass decision. The existing
250 ms limit, source validation, functional checks, resource audit and explicit
negative control are unchanged. A child failure still fails its calling test.
No retry or acceptance override is introduced. Source/runtime tests are not
proof of actual NAS latency, physical staging acceptance or reboot recovery.
