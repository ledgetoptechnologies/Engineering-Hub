# Reusable executor connection check

This explicit container-shell check starts no service, database owner or workflow.
Actual NAS routing remains untested; source tests are not deployment acceptance.

`engineering_hub.executor_connection_check` uses the shared
`ContainerPilotClient.probe_connection` and existing protected file reader.
It becomes available in the container only after the package containing this
module has been published and explicitly deployed.

The method issues exactly two GETs to the same explicitly supplied fresh canonical
command UUID. First it removes Authorization from the built request; second it
preserves the dedicated caller TEST bearer. Only exact401 `unauthorized` then
404 `command_not_found` JSON is accepted. Unexpected statuses, redirects,
duplicate JSON keys, token echoes and over16KiB decoded bodies fail closed. One
exchange/body deadline of at most10 seconds covers both requests and their body reads.
Each response close has a separate cooperative allowance of at most
`min(1 second, configured client timeout)`; CLI client close has another1 second.
Cleanup timeout/failure fails the check, never silently reports success, and
an existing caller cancellation is preserved. There
are no retries, POSTs, reconcile calls, database/owner operations, admissions or
task registrations. A failed first request prevents the second.

The CLI reads explicit protected credential and public CA files through
`unified_pilot_startup.read_protected_file`: credential current-UID0600 under a
private0700 parent, no-follow stable file reads and trusted ancestors. It checks
the supplied SHA256 digests privately, then delegates all TLS policy to
`ContainerPilotClient.for_verified_https`. It does not print credential values,
digests, file paths, response bodies or detailed exceptions. No agent environment
tokens, environment CA/proxy discovery, or production credentials are needed.

Failures include only a fixed local `phase`: `arguments`, `credential_read`,
`ca_read`, `material_validation`, `tls_client_setup`, `transport_auth_probe`,
or `client_cleanup`. This identifies which check needs inspection without
reflecting inputs or exception text. A probe failure remains the reported phase
if cleanup also fails; phase reporting does not diagnose the underlying TLS,
network or authorization cause and never authorizes an automatic retry.

## Operator invocation after publication and separate approval

Run inside the actual NAS Hub container as its normal UID. Provision the CA and
dedicated TEST credential privately first; this CLI never creates/chmods them.
The following is a template, not an activated command or an instruction to copy
real token values into chat:

```text
python -I -B -m engineering_hub.executor_connection_check \
  --origin https://192.168.50.90:9443 \
  --ca-file /var/lib/hub/private/executor-ca.pem \
  --ca-sha256 sha256:PUBLIC_CA_DIGEST \
  --credential-file /var/lib/hub/private/executor-test-token \
  --credential-sha256 sha256:LOCALLY_VERIFIED_CREDENTIAL_DIGEST \
  --unknown-command-id FRESH_CANONICAL_LOWERCASE_UUID
```

The credential argument is a protected file path, never the bearer itself.
Use the actual approved origin/certificate SAN and locally verified digests.
Do not disclose credential-derived digests as a substitute for protecting the
credential. Do not remove hostname verification to make a LAN-IP test pass.

Success means **transport/auth verified only**. It does not establish executor
provenance, correct native ownership, resource limits, machine idleness, healthy
baseline, task enrollment, workflow acceptance, rollback or restart recovery.
No actual agent/admin identities are inspected, so the result explicitly says
`agent_token_reuse_checked: false`. Dedicated TEST provenance must be established
by the separately reviewed credential provisioning process; the CLI never claims
that a working bearer is not an agent/admin token.

Use a UUID known not to be an existing command. Receiving another status fails;
the CLI never substitutes a new UUID or retries after ambiguity. Preserve and
inspect the failed result rather than resubmitting work.

## Local checks performed

26 pure/mocked pytest cases cover exact request/auth sequence, unexpected or
redirect statuses, duplicate JSON, oversized body, echo, nonfinite JSON,
wrong second response, one shared body-read deadline, invalid UUID, protected
reader/factory wiring, closed flags/sanitized errors, digest mismatch before
transport allocation, slow response/client cleanup, cleanup failure and caller
cancellation preservation. Three additional loopback TLS cases exercise the
actual shared transport and new two-GET probe: matching trust/name accepts the
synthetic401/404 protocol responses, while wrong CA and wrong hostname fail
before any HTTP request reaches the test handler. Those are real local TLS
handshakes, not an enrolled executor or NAS-route test.

The combined Windows client/startup/renderer focus passed158 cases;17 Linux-only
permission/ownership cases were skipped and are not claimed covered. Linux
validation and the actual NAS container's permissions/network route remain
separate acceptance requirements. Existing shared HTTPS/protected-reader tests
also cover their established source-level contracts.

These are cooperative asyncio/HTTPX budgets, not a hard whole-process deadline.
Synchronous protected file reads and TLS-context setup precede the asynchronous
exchange deadline. Local malicious transports that suppress cancellation, or
blocking filesystem/kernel operations, are not bounded by these coroutine
deadlines. No subprocess/watchdog or alternate client runtime is introduced.

No external/LAN network operations, SSH, builds, installs or deployments are
performed by source validation of this module. Its wire tests use local
loopback TLS only.
