# Software staging checkpoint — 2026-10-18

## First-use readiness audit

Initial readiness-audit source: `0a40d88`; newer verified source checkpoints
are recorded below. The original goal's GitHub
human sign-in wording was superseded by Beau's separate Hub Cloudflare Access
choice. Do not add a second GitHub sign-in flow to satisfy the stale wording.
No current live installation or authenticated browser session was inspected in
this audit; earlier source/test evidence is not fresh deployment evidence.

| Requirement | Current evidence | Remaining first-use gate |
| --- | --- | --- |
| Shared task, review and decision records | Repository/API/dashboard source and earlier isolated HTTP tests | Verify installed release and authorized human UI against the intended database |
| Browser-only tunnel, LAN agent interface | `server.py` binds browser loopback and separate agent socket; split tests recorded below | Beau-owned NAS/tunnel deployment and actual allowed/denied browser checks |
| Software queue and production-first priority | `work_queue.py` and reviewed-target policy in `work_queue_config.py` | Default admission is deliberately closed; a queue flag alone installs no executor or production authority |
| Remote container execution | `ContainerPilotClient`, `OwnedPilotService`, `PilotASGI` and `HostAdapter` exist as explicit injected APIs | Concrete private host composition, protected build/baseline enrollment, trusted driver identity, installation and effective network/credential boundary |
| Local validation executor | `OwnedValidationService` composes a local `WorkQueue`, private receipts and native feed | It is not a remote queue client; do not mount Hub SQLite over SMB to make it remote |
| Health, rollback and recovery | Frozen state, observer draft, fixed probes, held outcomes and recovery source tests | Real isolated daemon/image/profile/health/deploy/rollback and installed restart acceptance; drafts are not approved enrollments |
| Agent topic availability | Registry client/bridge and evidence-aware preflight source | Canonical registry injection and actual participant session/runtime evidence; transport subscription is not an awake agent |
| Preserve existing MCP data and compatibility | Old 8765 service left untouched; offline archive validator integrated with frozen legacy fixtures | Operator-produced coherent read-only bundle, actual validation and separately authorized retrieval/copy; no replay of archived messages |

The next real pilot requires an approved isolated Linux executor boundary.
Existing shared staging Viewer/PA containers are not a disposable test boundary.
Read-only local inventory found only Docker Desktop's internal WSL distribution,
not an existing general-purpose Linux operator environment. No internal WSL
administration, socket mount, VM provisioning or shared-host service change was
performed to bypass this prerequisite. Beau chose continued source-only work
and clarified that the design must use the existing equipment. No additional
PC or dedicated VM is a prerequisite. Future activation should assess an
appropriately constrained test setup on the existing staging PC, separate from
production data and processing workloads; a Compose project name alone is not
a security or physical-resource reservation boundary. No host provisioning,
private credential/context change or live service activation follows from this
hardware clarification. Existing local Docker remains suitable for bounded
synthetic tests. Host selection is not evidence that an executor exists.

## Latest verified source

### Owned host-executor listener composition — 2026-10-19

Beau asked Codex to continue available local work while Hermes has MCP connection
issues. New `hub_container_executor/service.py` supplies explicit owned loopback
serving around the existing `PilotASGI` and `HostAdapter`; it does not start from
ordinary Hub configuration. The socket binds before a synchronous operator
resource context is entered. Exact adapter type, known open/current-process/idle
state and non-null provider checks deny obvious invalid compositions, but do not
prove fresh exclusive ownership, provider authenticity or host enrollment.

The service closes new HTTP admission on stop, drains accepted HTTP/raw executor
work through repeated caller cancellation, closes the accepted adapter off-loop,
then exits the provider context off-loop. The trusted context owns partial entry
failure and provider cleanup, including attempts after individual cleanup errors.
No context suppression can turn a failed listener into a successful service.
Uvicorn can close the listening FD before accepted work drains: port closure is
not evidence of terminal work or ledger release. No new queue, claim adoption,
automatic reconcile/replay, dynamic module/config loader or host discovery exists.
See [`staging-executor-service.md`](staging-executor-service.md) for the contract.

Code/test commit `e86520b5039cd4b956628abab7d692f2db343929` initially produced
**2 failures, 149 passes**, 13.96s in a strict focused run. One restart fixture
mistakenly minted new build invocation IDs and thus a different manifest; test
follow-up `8303c5b4ddaf5240810ef3eaee8c01df72988f9b` instead reopens the original
exact synthetic enrollment with new provider objects. No production guard or
target identity was relaxed. The second issue is the older mixed async/sync
test-loop warning: old image `a91324d...` independently reproduced **1 failure,
36 passes**, 6.51s with the existing queued-service tests followed by ASGI tests
and ResourceWarning/unraisable warnings as errors. Its five ASGI tests alone
passed, 0.34s. No warning filter or existing test was changed to hide it.

Clean `8303c5b` archive image index
`sha256:0863db4d386689fc660ebfdbe35a55003739d89854f9e6547fd9c4ea9c5ecd45`,
platform manifest `sha256:5e0bd4e69115b8e8423e17cb63d1cf473c47f0d58aa76a686f10daa30b47135c`,
config `sha256:c2e8f77a35a9217eebe70b9893e39025ff09adea7f442219e852d40c7cacfec1`:
**146 focused checks passed**, 13.60s, with ResourceWarning/unraisable warnings
as errors and no warnings; six CHOWN-only ownership cases passed, 0.58s.
The initial full validation run had **2 failures, 3516 passes, 6 skips and
4 known warnings**, 173.19s. Both failures were existing fake executable tests
blocked by the validation container's `/tmp` no-exec mount. The same two tests
passed **2/2**, 1.13s with explicit executable `/tmp` in the disposable validation
container. This changes no application/pilot runtime profile: its closed target
tmpfs remains no-exec. Two initially mistyped owner-test selectors collected no
tests; the corrected six-case run above is the evidence. Containerd identifies
the runnable local image by the OCI index, not its config digest; initial config-
ID launch attempts found no image and started no tests.

Independent source/test/documentation review accepted the service and its 48
new cases. Test commit `7354750fe9db7359d120f65c271add9b982b9928` adds three
actual controller-to-executor integration cases. Public TCP, protected browser
UDS, private coordinator TCP, native queue events and executor TCP are real.
Human task creation uses a signed synthetic Access assertion; Codex cannot
self-review, and the distinct Claude fixture reviews the exact source before
submission. Test-only operator admission is restricted to that actual task,
machine, action, source and artifact, not a blanket permissive callback.

The healthy and failed-candidate/healthy-rollback paths verify controller run/job
rows against host ledger receipts and original-command HTTP GETs without extra
effects. Cancellation during an actual remote phase drains the accepted host
command but leaves the controller job held and its machine reservation retained;
no follow-on probe or POST replay is inferred from host command success. Provider
barriers are released before cleanup even on failed assertions. These remain
synthetic daemon/build/baseline/health/state providers, not real Docker rollout.

Final clean `7354750` archive image index:
`sha256:9e6313574745eef5562874406ec0ff140f02c734da12bbddf180fcf723f99da4`;
platform manifest:
`sha256:37be9f75528fd169d668b9fd2e4c9809fa1e40ecdf7f0dd921a8ffcc4661100a`;
config:
`sha256:5d394a7d335d0789e398e617e125f1087af7e3b6267ede6fb302c4fdd2c6956a`.
The three integration cases passed, 4.83s. Combined new service/integration and
existing private service/split/public lifecycle checks: **149 passed**, 17.55s,
ResourceWarning/unraisable warnings treated as errors, no warnings. Separate
CHOWN-only owner checks: **6/6 passed**, 0.57s. The unchanged two-container gateway
isolation harness passed both healthy and expected-rollback cases; it does not
run the new executor composition across containers. Owned smoke containers and
volumes were removed and absence verified.

The final full exact-image Linux suite completed: **3521 passed, 6 skipped,
4 known warnings**, 175.16s. Warnings are Starlette, the older mixed-loop test
fixture and two deliberate threaded-fork checks. The disposable full-suite test
container explicitly permits `/tmp` execution for its fake executable fixtures;
it remains network-none/read-only/capability-dropped, with no host mounts or
published ports. No pilot target runtime restriction was relaxed. All build/test
execution handles are terminal; this is not PC-reboot or installed acceptance.

Nothing was installed/activated on staging or TrueNAS, and no live MCP/ports,
tokens, credentials, Access policy, production deployment or listener changed.

### Staging runtime inventory received — 2026-10-19

Hermes completed the approved narrow read-only inventory in reply
`ad625a54-f4e5-44f7-9340-310edb8ac167` to
`de1534f4-623f-426f-8955-9f02fb32e322`. This is peer-reported runtime evidence,
not a second Codex SSH inspection or executor enrollment. Prometheus remains
the device-capacity source; no duplicate hardware inventory is needed.

The report identifies Docker 29.5.2 / API 1.54 (daemon-reported minimum 1.40,
not the pilot feature floor), daemon
`4a562438-a128-4837-acb6-f85fd207ebee`, on the staging PC. Its one shared
rootful daemon hosts two PA web/cron/database stacks, Viewer API/worker/proxy
and node-exporter: 21 containers (10 running, 11 exited) and 15 local named
volumes at the observation. No NodeODX container or existing separate/rootless
daemon was found in the checked locations. These are snapshot observations,
not proof of absence across every possible context or future scheduled work.

Viewer API/worker share writable `/mnt/ViewerStaging` CIFS and a nested local
data overlay. PA web/cron share their project volumes, with distinct database
volumes. PA containers have no configured CPU/memory/PID limits. Viewer limits
are API 1 GiB / 2 CPU / 256 PIDs, worker 1 GiB / 1 CPU / 256 PIDs, proxy
128 MiB / 0.5 CPU / 64 PIDs; configured limits are not reserved spare capacity.
Do not sum those limits or infer idle jobs from healthy web endpoints.

`viewer-staging.service` was stable/active, with no restart and no active
rollout observed; its long-lived Compose supervisor is not a new deployment.
`/etc/cron.d/staging-image-watch` invokes `staging-image-watch-run`, whose
behavior has not yet been inspected. A cleanup timer was active but not
executing at the snapshot. Both remain potential writers; an idle snapshot
cannot exclude their next invocation. The separate PA egress service was
reported failed, with its timer active; it was not changed or repaired.

The next host-readiness evidence is therefore **writer behavior and a bounded
pilot boundary**, not another hardware scan. Before activation, inspect the
image-watcher/cleanup selectors and actual Viewer/PA job state under appropriate
read-only authority; identify how concurrent starts, cleanup and deploys will
be excluded or coordinated for the exact pilot resources. Do not infer that
inspection approval permits stopping timers, changing limits or provisioning.
On the existing equipment, propose a disposable test-only instance with dedicated
local state, network, image/volume ownership and explicit resource budget,
without mounting existing application data or exposing the shared Docker socket
to agents or the browser. Effective authority and physical-host contention must
be assessed; a Compose name, label or separate daemon alone is insufficient.
The current report does not prove that boundary or authorize enrollment.

This inventory did not start, stop or modify host jobs, provision or install
anything, change credentials/permissions/mounts, perform cleanup, interrupt or
restart services, or manage TrueNAS. The
inventory request is answered; pilot execution, actual immutable application
health/rollback and installed restart acceptance remain incomplete. Hermes's
ongoing adapter work is not duplicated or treated as a verified running handle.

### Machine-wide production precedence — 2026-10-19

Hermes characterization `9fc1fee6-9499-475c-8d49-a166e665e7af` exposed an actual
priority gap: action-filtered claims could take staging on a physical machine
while approved, current production for another action remained queued. Independent
regressions against the old source reproduced **3 failures, 8 passes**, 0.81s.
The old worker-local admission expectation was deliberately corrected to the
requested cross-workload physical-machine policy; no approval check was relaxed.

Tested source `7579b06c9e5d752aaa10c0ad54b4a8bba2043d51` uses one production-first
ordered claim pass in the existing SQLite transaction. Production registry,
approval, review/target and readiness observations precede action/worker filters.
Ready or unknown valid production blocks new same-machine staging regardless of
which worker can claim it. Other physical machines proceed independently. Removed,
rebound, stale or unapproved production does not become a barrier. Already-running
work, lease/fence ownership, renewal and authoritative held reconciliation remain
unchanged. Selecting production immediately avoids a temporary two-pass design's
expanded cached-policy window; external policy snapshots are not atomic.

The optional private `production_readiness_verifier` provides worker-independent
readiness only, not approval or worker authority. Literal False excludes that
production candidate from both new production claims and staging precedence;
configured unknown/error/non-Boolean denies new production and keeps the staging
barrier. Without a provider, existing production worker admission remains, while
unknown global readiness conservatively blocks new staging. No provider or host
readiness is fabricated; unknown can intentionally delay staging. The callback is
admission-only and is not invoked by status, renewal or accepted-work continuation.
Unstarted coroutine results are closed; foreign started coroutines/Futures are not
cancelled or closed. Public schema, environment activation, identities, registry
and persistent job schemas are unchanged. Trusted compositions must share one
canonical action/machine registry, not hide production with per-worker registries.

Independent source/test review accepted the final single-pass implementation,
34 priority regressions, the changed legacy expectation and actual HTTP/MCP filter
parity cases. Windows focused checks before the final loop simplification:
**158 passed**, one known Starlette warning, 9.16s; the final loop's core/priority/
admission checks: **95 passed**, 3.91s. The clean final-source Linux image index is
`sha256:a91324d865756d6afcb87f4b9426d150b810687dd7ce50af881add78d0695aba`,
manifest `sha256:61ac9fff6b77c9617387e284733d4d9507de476c98cd28e0fc5a77a8de0a6a67`,
config `sha256:b1f5c2f4c08f6590a1d26b67bc28712930083cedfe6d97131e87e4731a7126d8`.
Focused exact-image queue/API/MCP, bridge, owned-service, runtime and gateway
checks: **232 passed**, one known Starlette warning, 12.69s; ResourceWarning and
unraisable warnings treated as errors. Separate CHOWN-only ownership cases:
**6/6 passed**, 0.59s. Both existing disposable two-container synthetic healthy/
rollback cases passed; their owned containers/volumes were removed and absence
verified. These tests do not prove a real production job or enrolled runtime.

The final exact-image full Linux suite completed: **3470 passed, 6 skipped,
4 known warnings**, 169.73s. Remaining warnings are Starlette, one older unclosed
test event loop and two deliberate multithreaded-fork deprecations. No test failed
on the final image; all execution handles are terminal.

Dashboard eligibility already states advisory/conditional, not claim readiness.
The new callback/priority barrier is evaluated at claim, not by read-only status;
this limitation is explicit in the agent quickstart. No new SDK, scheduler,
polling worker, host provisioning, production approval, deployment, MCP repair,
NAS management, port/token/config change or old-message replay occurred.

### Read-only Engine compatibility and live wake blockers — 2026-10-19

Hermes reply `9741d54c-b406-48b1-b237-8515dbf95b12` reports successful actual
`scripts.hub_engine_runner_probe` / `DockerEngineBackend.inspect_runner(5000)`
on source `7e061e0968ff7e06c99990c5be5589272a35a16d`; Hermes also reports a
supervising-agent rerun. This was the existing **Hermes host**, not staging: Docker 29.5.2,
API 1.54, Linux/amd64, matching the explicitly pinned daemon identity. Source
inspection confirms the probe uses GET `/v1.54/info`, closes its HTTP client and
does not provision the inert generation-root binding. No probe/backend source
changed between that commit and this checkpoint. Hermes used existing Python
3.14.7/httpx 0.28.1/pydantic 2.13.4, not the exact locked environment. This is
peer-reported real runner compatibility, not independently rerun by Codex, full
driver/deploy acceptance, an approved isolated boundary or staging configuration.

Hermes replies `a4b1945a-bfc6-4f04-915a-c59fc2218034` and
`55176fe8-a6b1-42cb-b246-9dccbdabd4d7` identify separate wake blockers. Codex
independently read existing `http://192.168.50.80:8765/health`: version 0.6.0,
schema 4, `database: ok`, `ok: false`, push `failed`, subscriptions disabled,
backend `linux-inotify-sqlite-wal`, reason `OperationalError`, cursor 696.
Successful database reads/message storage do not prove notification delivery.
The exception class alone does not establish the underlying SQLite failure;
existing sanitized operational error details are needed before choosing a repair.
No restart, toggle, permission change or database repair was attempted.

Hermes separately reports its saved topic binding points at ended session
`20261017_141423_9252c9`; another stored desktop session is not proof of the
owner's intended destination. Current listener state was deliberately not probed
through an endpoint that could hot-start the backend. No canonical topic registry
origin or current end-to-end ready session was independently verified. Repairing
server events alone would not validate the desktop binding. Owner-confirmed
mapping and explicit correct-session idle/busy/restart tests remain necessary;
do not blindly replay old uncertain dispatches. Closing Hermes Desktop at Beau's
request does not authorize reopening or rebinding it here.

Device capacity is already supplied by Prometheus. The original approved staging
inventory is now narrowed to Docker/API, existing workload/volume/writer boundaries
and practical isolation on existing equipment, not a duplicate hardware inventory.
Follow-up `9a793132-fa96-462f-890c-b24acf1e659c` conveys that refinement without
adding host/TrueNAS/configuration/activation authority.

### Existing Hub reported build — 2026-10-19

A read-only GET to `http://192.168.50.80:8770/health` returned `ok: true` and
`build_commit: c720f7a82c70230e267a369077211749c30e9360`. The local Git object
identifies that commit as the 2026-10-18 control-V1 merge, and confirms it is an
ancestor of the tested development branch. The later queue binding, native pilot
runtime and owned coordinator commits (`33e0206`, `c05557f`, `2ef4333`) are not in
that reported source revision. Do not present development-branch tests as proof
that those additions are installed. This HTTP observation does not independently
verify the image/provenance, private composition, human sign-in or executor.
No pull, redeploy or configuration change was performed. The later staging
inventory above answers the narrow host-runtime request, but does not establish
an isolated executor boundary or replace separate pilot approval.

### Read-only staging telemetry and module assessment — 2026-10-19

Beau separately approved a read-only readiness inventory and supplied Prometheus
at `http://192.168.50.80:30104`. Codex queried its existing HTTP query API only;
no SSH or host-management access, installs, mounts, container effects or service
changes occurred.
The staging target `192.168.50.90:9100` was reporting `up=1`. Query evaluation
around 2026-10-19 14:06 UTC showed:

| Observation | Value | Scope limitation |
| --- | --- | --- |
| Logical CPUs | 6 idle-mode CPU series | Not a CPU model or reservation |
| Total / available RAM | 8,185,819,136 / 5,543,014,400 bytes (about 7.6 / 5.2 GiB) | Point-in-time availability, not a job allocation |
| Root filesystem | ext4, 105,089,261,568 bytes total; 15,000,563,712 available (about 14.0 GiB free), reported writable | Exporter observation, not mount/identity/durability acceptance |
| `/mnt/Staging` | CIFS from `//192.168.50.80/Staging`, 911,607,070,720 bytes available | NAS data storage, not approved local SQLite/owner state |
| Other staging mounts | Two CIFS paths under `Ai_Storage/Shared/Staging`, each reporting 1,077,750,398,976 bytes available | May share underlying capacity; do not add these values or assume separate quotas |
| Platform / sample freshness | Linux x86_64; memory sample approximately 15.7 seconds old at the follow-up query | Does not prove Docker API, driver identity, network isolation or agent-session availability |

Root disk headroom must be included in the proposed pilot budget: image layers,
build cache, candidate/baseline state and logs can exceed container scratch limits.
Do not authorize a build/pull/cleanup from these readings. Do not move the private
controller/runner SQLite databases to CIFS to bypass local storage requirements.
Hermes's direct inventory request is still awaiting a relevant reply in the
returned inbox scope; telemetry is not a substitute for that host evidence.

Hermes module-design reply `99fa8d6c-b50d-47e7-beab-adf506aebe4d` was claimed and
evaluated against the earlier Claude proposal: both favor local Git workspaces,
immutable candidate/test evidence and declarative fixed actions rather than
unversioned uploads or hot-loading plugins into Hub/MCP. Privileged adapters stay
behind typed private interfaces; UI panels initially use reviewed Hub builds.
GitHub need not be a roundtrip for every local edit, but local Git is not an
off-host backup. This is architectural agreement, not implementation approval or
proof of an installed module platform. Prioritize one real bounded pilot before
adding extension distribution/hot swapping. No new module service was created.

### Owned queued-coordinator source — 2026-10-19

Implementation `2ef4333d66223f991e88967e4c1722e89779b9ce`; final tested source
`6e364635b4be5c5afd3499ee0eccff8ac3f91a46`. Clean Git archive image index
`sha256:880604187c9226d02f68eece60ac2fc9c31dff944931dd13e441debfff3cd1ec`,
manifest `sha256:b50f28a984e05e0023939c30f70bea500c578127f5d5e774fa77cae156d58d70`,
config `sha256:e5e22e109292821c56e86bc5034ca5004b044950dd42e134151028de51c29262`.
Focused service, public/private gateway, split runner, runtime, ownership-lifecycle
and push-worker tests: **252 passed**, 16.60s, ResourceWarning and unraisable
warnings treated as errors, no warnings. Separate CHOWN-only ownership cases:
**6/6 passed**, 0.57s. Both existing two-container healthy/rollback cases passed;
their disposable containers and volumes were removed and absence verified.

Final exact-image full Linux suite: **3434 passed, 6 skipped, 4 known warnings**,
167.51s. Remaining warnings are Starlette, one older unclosed test event loop
and two deliberate multithreaded-fork deprecations. All execution handles are
terminal; the focused lifecycle/push tests above remain warning-free.

The new explicit API binds before constructing one persistent owner, exact
dedicated client, staging-only Engineering Hub queue/bridge, native runtime and
private core. The retained operation drains before ordered client/owner cleanup
attempts, including repeated caller cancellation. A mandatory synchronous veto
adds no implicit host enrollment or restore safety. Thirty-two new Linux service
cases exercise real TCP/UDS/auth/queue lifecycle with synthetic executor effects;
the production API has no ephemeral-storage override. Independent source/test
review accepted the implementation and corrected cleanup-failure documentation.

Initial source-image verification exposed two test-fixture issues. Synchronous
public CLI unit tests cleared pytest's idle loop reference; scoped test Runners
now preserve it without altering production asyncio startup. Read-only diagnostic
paired tests confirmed before/after behavior. The initial full suite had **1
failed, 3433 passed, 6 skipped, 6 warnings**, 168.22s: the push fixture manually
published alongside the native source and incorrectly required exactly one
notification. The corrected fixture waits for actual bootstrap catch-up, relies
only on native delivery, preserves exact one-message work assertions and drains
the SDK worker before server teardown. No product notification/coalescing or
authorization rule was weakened.

The existing two-container smoke still uses its synthetic private composition;
it is not cross-container proof of the new owned-service API. Neither test proves
real immutable application deployment, health/rollback authority, TrueNAS
configuration, reboot/power-loss recovery or machine-wide restored-DB safety.
Private transport, independent enrollment/baseline/provenance, host writer
exclusion and actual installed acceptance remain gates. Existing entry points,
Compose, old live MCP ports/tokens/data and host services are unchanged. Beau's
existing-equipment/source-only direction requires no extra PC or mandatory VM.

### Packaged standalone public gateway — 2026-10-19

Tested source `c13d3599a9f5b4b5a41f7fc3d1316acbd764741f`. Clean Git archive
image index
`sha256:ec3b2c7bac174182e19b8d257f62ea61572738848bcb955b0e167f240f0555e6`,
manifest `sha256:9044298260db7c8348ef6aeca39e971d8a1cc2cfec84bd9cd9334b119bcc4920`,
config `sha256:04583b30f26ab79843def6aa4401d87b395a4f7c37749b459323375d804e1d08`.
Focused CLI, gateway, split runners, native runtime and owned-task tests:
**193 passed**, 9.76s, ResourceWarning errors enabled, no warnings. Separate
CHOWN-only ownership cases: **6/6 passed**, 0.59s.

Full exact-image Linux suite: **3402 passed, 6 skipped, 4 known warnings**,
162.37s. Warnings remain Starlette, one unclosed test event loop and two deliberate
multithreaded-fork deprecations. All handles are terminal. Initial Windows import
guard failure exposed the dependency described below and passed after the helper
extraction; no new Linux product-test failure occurred at this checkpoint.

The two-container smoke now launches the actual installed module
`python -m engineering_hub.public_browser --socket PATH --port 8771`, not a
test-only public service function. Both healthy and rollback cases passed on
the exact image above, retaining private-state ENOENT, read-only socket EROFS,
separate namespaces and real native-queue workflow checks. A new kernel socket
probe verifies only IPv4 loopback listens on 8771, with no IPv6 listener. Readiness
uses a real 401 from the allowed unauthenticated auth/me route across UDS, not an
invented public health endpoint. Executor/health outcomes remain synthetic.

The new CLI takes explicit normalized socket/port arguments, has no private
environment fallback and sanitizes argument/startup errors. Import/help are
inert. An isolated import guard initially exposed private repository dependencies
through the shared cleanup module. `finish_owned` was moved text-identically to
state-free `lifecycle.py`, preserving the same-object `owned_tasks` export; the
private frontend import is now local to its runner. The guard now proves public
runner/client imports do not load app/repository/pilot-owner modules. Existing
cancellation, repeated-stop and drain tests remain part of verification.
Independent source review accepted the CLI and exact helper extraction.

The original `__main__`, production Docker entry point, Compose and health
checks are unchanged. They still select the older combined process; they do not
activate this CLI or an autonomous private factory. A future approved deployment
needs explicit entry-point/readiness wiring and real private manifest, transport,
baseline and host enrollment. No production factory was synthesized from tests.
No live NAS, MCP ports, tokens, services, permissions or Access policy changed.
Disposable smoke containers and volumes were removed and absence verified.

### Separate-container browser/private-core boundary — 2026-10-19

Source fixture `3536565`, direct-import/capability correction `800993e`, and
deterministic cursor regression `4ce214b7c65ff7a60baae341755dd5b8c786081b`.
Clean Git archive image index
`sha256:3969b9f38eeeec007699fcdcdb094737504fe993f6b345ec7828057a464a9de5`,
manifest `sha256:9f5c519946f585af643a44687d1148e18b4076e498b0ddae802c626360edc177`,
config `sha256:491b925601406cc2e4e4d45d8e73e0d47bcbadd9aa10a76e5946b0544de5d574`.
Focused gateway/harness/split-runner/cursor suite **136 passed**, 10.58s,
ResourceWarning errors enabled, no warnings. Separate ownership cases **6/6
passed**, 0.59s. Independent review accepted the harness, fixture and test fix.
The full exact-image Linux rerun passed **3369 tests**, with **6 skipped and
4 known warnings**, 160.77s. Warnings remain Starlette, one unclosed test event
loop and the two deliberate multithreaded-fork deprecations. All test and smoke
handles are terminal; there is no background worker from this checkpoint.

The unmodified `scripts/hub_private_gateway_isolation_smoke.py` completed both
healthy and rollback cases on that exact image. Each case creates fresh,
labelled disposable containers: network none, private PID/mount/IPC namespaces,
read-only root, dropped capabilities, resource limits, no host binds, published
ports or Docker socket. Their only shared volume contains the strict Unix
endpoint and is read-only in the public container. Private database, randomly
generated synthetic agent bearers and executor canary exist only on private
temporary storage. The browser demonstrably gets ENOENT for the private paths
and EROFS on a socket-directory write attempt, yet real HTTP over the Unix
connection succeeds. Namespace identities differ and private processes are not
visible from the public fixture. Public repository/owner constructors are also
guarded by test tripwires.

Real signed synthetic Access assertions, session/CSRF task creation, separate
agent identities for claim/candidate/review, native queue scheduling and browser
result reads cross actual container processes. Typed host effects and health
proof remain synthetic: this is not a real application deploy/health/rollback
pilot or Cloudflare sign-in acceptance. A read-only socket mount prevents
replacement through that mount, not all possible privileged-writer attacks or
cryptographic private-peer authentication. Installed NAS/tunnel policy and
reboot/power-loss acceptance remain unverified.

Initial smoke attempts stopped on Docker's equivalent CAP_CHOWN spelling and
direct-script import-path setup; neither loosened the runtime permission limit
or application auth. A diagnostic run located the import issue; only subsequent
unmodified harness passes are acceptance evidence. The initial clean `3536565`
full run was **1 failed, 3367 passed, 6 skipped, 4 known warnings**, 176.66s:
an existing cursor test's 0.15s sleep did not prove its real SQL head snapshot
preceded an external process commit under load. The test now synchronizes that
actual read and commit, asserts exactly one original head value, and retains
the original API timeout and result checks. No production feed code changed.
All labelled smoke containers/volumes were removed and absence verified. No
live MCP, NAS, credentials, policy, production or staging-host service changed.

Claude reply `bdd6423d-65f7-47ff-96b3-86fb7e282321` now accepts endpoint
lock plus generation provenance and correctly notes that deployments using the
strict default still refuse residue. A future approved deployment profile must
explicitly select recovery; source acceptance does not activate it. Startup
refusal also needs operator-visible supervisor/readiness diagnostics: a process
that never starts cannot report its own HTTP health. That deployment/visibility
work remains pending, not implemented by acknowledging his proposal.

### Opt-in published endpoint recovery

Source `b301ffa9a0d7503b2fa230f5b9dbd5cdf239c4b9` adds an explicit,
default-off recoverable Unix endpoint to the split private runner. Clean Git
archive image index
`sha256:e975111951b75069820ca84fe743507d51eb35f35ea27689d0d09553e11eadd1`,
manifest `sha256:bc373398926c419107d6a8aa9c3c545da9c91d5d4f1f684138e370157db348f6`,
config `sha256:95160814ca6f1a84c0a17972ed9c6ba14b3eead767ca93669c1c02f44770c459`.
Focused recovery, split runners, queue runtime, strict transport and contract:
**112 passed, 3 skipped**, 17.33s, ResourceWarning errors enabled, no warnings.
Separate CHOWN-only ownership cases: **6/6 passed**, 0.59s.
The initial full run was **1 failed, 3326 passed, 6 skipped, 4 known warnings**,
162.29s: an older archive fixture left a SQLite connection open, so its
no-WAL assertion failed after checkpoint. This is not a green full-suite result.

The 36 new recovery cases include real subprocess SIGKILL, publication-cut
boundaries, live competing ownership, malformed/replaced identity preservation,
fork descriptor closure, and reconnect through the same HTTPX client and browser
gateway after the private endpoint restarts. An endpoint-specific flock and
durable exact generation record authorize removal only of the matching stale
socket. Unknown or prepublication residue is preserved and denied. Defaults and
the original runner are unchanged; no service or configuration was activated.

Independent source/test review accepted this scope. Claude's processed reply
`d1624ea9-dcb5-4981-9be7-4aabf552a6e9` agrees about inherited-listener closure
and no job/lease authority, but proposes connect-probe-only stale detection.
A separate isolated Linux experiment retained an open, bound, not-yet-listening
Unix socket: connect returned ECONNREFUSED while both its owner FD and pathname
still existed. Thus refusal alone is not ownership provenance, especially with
the runner's bind-before-owner-factory ordering. The conservative generation
protocol is retained; this is not represented as peer approval of that protocol.

These tests establish process-crash endpoint recovery after publication, not
power-loss durability, PC reboot, installed supervisor behavior, separate
container credentials/mounts, job replay, machine reservation release, or a
machine-wide restored-database barrier. No live NAS, MCP ports, tokens, Access
policy or production workload changed. Existing equipment remains the target.

Test-only follow-up `fd32522f18afb5c05c19c6360ad645840bfb4929` explicitly
closes connections opened by the unchanged frozen archive initializer. Frozen
resource hashes, checkpoint and no-sidecar assertions remain unchanged. An added
regression disables cyclic garbage collection and checks database integrity and
absence of WAL/SHM. Independent review accepted the fixture-only change.
Clean archive image index
`sha256:a6400665b2ee3c8d42cebef066e05b42703d759ce4609a85f003d079456f9154`,
manifest `sha256:59b945d340aa29bdb6ca7a25b15a211154105f54b10179a2272c16a6ff3aa3c9`,
config `sha256:426e59ff536ab9581f85f9ae099d78c7dc808940164bd3f94d0fd1b6f2d3fa96`:
expanded focused archive/recovery/runner/runtime/contract checks **143 passed,
3 skipped**, 20.73s, ResourceWarning errors enabled, no warnings; separate
CHOWN-only ownership checks **6/6 passed**, 0.81s.
Full exact-image Linux rerun: **3328 passed, 6 skipped, 4 known warnings**,
165.52s. Warnings remain Starlette deprecation, one unclosed test event loop
and two deliberate multithreaded-fork deprecations. All execution handles are
terminal. This successful rerun includes the new deterministic fixture regression;
the initial failed result above remains recorded rather than overwritten.

### Split listener lifecycle and immediate admission veto

Tested source `2ec018ae87a1a639a981a0d408f99643755854f5`, development branch
only. Clean Git archive image index
`sha256:054e7dc5d4ca1d884f2847d190fae0ad7175327b5b4c0144129be2727a7b36a3`,
manifest `sha256:c4eae379e56b9e79b6d02fb8dc90e486f50c63e2e48c43aecb3ec054f651411f`,
config `sha256:898db08c828eb25d716bd549f982dd576dde06ce29de8bc0658a00c9d819e491`.
Full exact-image Linux suite: **3286 passed, 6 skipped, 4 known warnings**,
152.42s. Focused split listeners, queued pilot, runtime, frontends, gateway and
contract checks: **104 passed**, 7.20s, ResourceWarning errors enabled and no
warnings. All six ownership cases separately passed **6/6**, 0.59s, on that image
with only CHOWN and synthetic fixture storage. Full warnings were existing
Starlette, an unclosed test event loop and two deliberate fork deprecations.

Two explicit source runners bind before factories: private agent TCP plus browser
UDS over one exact private core, and a separate loopback-only public gateway
accepting only its dedicated HTTPX client. Listener failure/callback failure or
repeated caller cancellation drains accepted requests before client/socket/core
release. Exact read-only event subscriptions receive a graceful disconnect;
ordinary requests and every POST are not force-cancelled by a shutdown timeout.
Actual pinned h11/ASGI stream behavior, not a generic future protocol promise,
is tested with open global SSE streams on both frontends.

Review found that listener drain alone would leave queue admission open while a
slow request finished. Explicit synchronous HTTP/runtime admission vetoes now run
before the first drain await. A real Linux owner/native-queue regression holds
one accepted phase and one accepted HTTP request, stops/cancels repeatedly, and
verifies the second job stays queued while feed/owner/socket remain alive. Only
the accepted phase finishes; its uncertain unfinished chain remains held.
Two additional full HTTP/UDS runner cases cover success and healthy rollback
using signed synthetic Access assertions and typed simulated remote effects.
Independent review accepted the wiring; the original combined runner is unchanged.

Initial working focus: **1 failed, 59 passed**, 7.00s. A new test sent an agent
bearer to human-only task creation and held its403 response, so it was not actually
testing an accepted mutation. The fixture now uses permitted agent decision
creation, asserts201 and persistence before blocking, then tests shutdown. No
authentication rule was relaxed to fix that test.

Claude reply `00bcac3e-8c6b-4717-82b8-b5a27834d880` correctly identifies that
stale socket residue prevents unattended restart. His proposed database flock
alone is not sufficient endpoint ownership proof: `PilotOwner` closes inherited
locks after fork, while a child can retain an inherited listener descriptor.
UID/mode/type are not a generation binding. Endpoint-specific lifecycle ownership,
durable exact identity metadata, inherited-descriptor handling and crash tests are
were next source work at this historical checkpoint; the opt-in implementation
and verification above now cover published endpoint generations. Per-container tmpfs alone does not
prove shared-socket reachability, namespace reset or reconnect. Unknown residue
still fails closed; no blind unlink or lease-expiry reassignment was added.

These tests do not establish separate container mounts/process credentials,
actual immutable Docker deployment/observed health, installed TrueNAS/tunnel,
independent enrollment, or app/PC reboot acceptance. No live host, MCP endpoint,
token, port, access policy, service or production deployment was changed.

### Unified signed-auth workflow over protected Unix transport

Tested source `b17cd728142d31e9aa0d65cba10b03c43d67ba88`, development branch
only. Clean Git archive image index
`sha256:8e5dd34fe20aad4d903637a9255b11057806304edfbf4aa690d934c0a62169cd`,
manifest `sha256:79735be2064d80f795a1577dcf89b286dfaa5aa7389a3286b8aaedb69a24711b`,
config `sha256:f9b29d04e0b95905efc9ac1e95403df81ae92b444c0cdf18b4e8ca95110fa0f8`.
Full exact-image Linux suite: **3250 passed, 6 skipped, 4 known warnings**,
152.81s. Focused socket, gateway, app, runtime, contract and unified-flow checks:
**90 passed, 3 skipped**, 4.52s with ResourceWarning errors and no warnings.
All six owner-change cases separately passed **6/6**, 0.59s, against that same
image with only CHOWN and synthetic fixture storage. Full warnings were the
existing Starlette deprecation, unclosed test event loop and two deliberate
multithreaded-fork deprecations. Independent source review accepted the changes.

Four unified cases combine real Linux `PilotOwner`, one private core lifespan,
signed synthetic Access assertions, browser sessions/tasks/decisions, original
source-IP agent authentication, independent candidate review, reviewed queue
submission, pause/unpause and native-event execution. Both in-process ASGI and
actual UVicorn/HTTPX Unix transport test success and healthy rollback. Remote
effect/health assertions remain deliberately synthetic; these are not real Docker
deployments or independent observed application health. Browser execution and
actor/executor spoofing, self-review, and a wrong IP with forged forwarding
headers remain denied. No second database writer or scheduler was introduced.

The explicit Linux socket helper requires a provisioned current-UID0700 directory,
safe root/current-UID ancestors and a new leaf. It creates a nonblocking0600 socket,
never adopts stale paths, and preserves replaced/unknown parent or leaf objects.
Tests cover permissions, symlinks, namespace replacement, child cleanup, actual
HTTP transport and foreign UID checks. Review identified that final-parent0700
alone would not protect against renaming through a nonsticky writable ancestor;
the source now rejects that ancestor and has regression coverage. Synthetic
CHOWN fixtures restore their own original directory ownership in teardown;
the library does not repair ownership or permission errors.

Actual protected process/mount composition still needs acceptance. Root or an
equally privileged same-UID process is not isolated by path checks. Crash residue
intentionally prevents a new bind, so no unattended restart acceptance follows.
Real host writer exclusion, independent provenance/baseline/health, remote pilot,
machine-wide restored-database reconciliation and app/PC reboot gates remain.
The existing live MCP, NAS, tokens, ports and production deployments were untouched.

### Private browser gateway and queued-core application lifecycle

Implementation `c9742e20dc2677b47f548abc2e4bcd8a4a59b174`; final tested source
`848667a8a33acac88234515001236f43e4245e7e`, development branch only. Clean Git
archive image index
`sha256:411942044db7d24be131c42f086fde9a9d63eb7a0bcda08eb1ac3e7897879059`,
manifest `sha256:dc61521036f6d244c9156f65a826a6607601789508af92f540b45aed803033ad`,
config `sha256:adfeb197c2c6f69aa0f5627a0158556b0af458e3d4f9f1d5a191f1bdd4c1dd69`.
Full exact-image Linux suite: **3225 passed, 3 skipped, 4 known warnings**,
145.42s. Focused gateway, app, runtime, DTO, queue bootstrap, owned service and
contract checks: **113 passed**, 4.81s, ResourceWarning errors enabled, one
Starlette warning. Three ownership checks separately passed **3/3**, 0.30s,
on the same image with only CHOWN and synthetic fixtures. Full-suite warnings
were Starlette, an existing unclosed test event loop, and two deliberate fork
deprecations. Independent review accepted the source, tests and contract diff.

The explicit app parameter binds one existing queued runtime to its exact
repository, queue and native feed. Startup requires the native source; shutdown
drains accepted effects before feed closure despite repeated cancellation. The
caller retains the owner/client until the lifespan exits. No environment flag,
service installation or replacement scheduler activates this composition.

The browser-only proxy and private receiver expose closed UI/auth/task/decision/
read/SSE routes and reuse core Cloudflare, session and CSRF checks. They do not
proxy MCP, private worker operations, direct dispatch or agent work submission.
Agents keep their original LAN listener and source-IP checks. Request and ordinary
response bounds, cookie isolation, no transparent mutation retry, immediate SSE
and cancellation cleanup have regression coverage. The public process must have
neither the database nor executor credential; actual protected UDS/process
composition is still an operator acceptance gate, not proved by an injected
test client. No host or live configuration was changed.

The 43 gateway tests use the actual core/repository and signed synthetic JWTs;
the eight app cases separately use real Linux `PilotOwner`/native events with
simulated remote effects. At this earlier checkpoint there was not yet a unified
gateway-to-enrolled-owner test. The newer source above adds that test, but not
an installed private listener or real remote pilot/reboot proof.
Machine-wide restore reconciliation and other activation gates below remain.

Verification history: the initial working focus had **1 failed, 104 passed**;
the app correctly denied unsupported startup, but the test expected an unwrapped
HubError rather than MCP/AnyIO's ExceptionGroup. The test now checks the actual
wrapped cause; no product guard was relaxed. Gateway test wrappers were converted
from nested asyncio.run to pytest-managed async execution to avoid losing the
test runner's event-loop ownership. Initial clean implementation suite had
**1 failed, 3224 passed, 3 skipped**, 145.29s: the checked-in OpenAPI snapshot had
not been regenerated for the new queued-runtime DTO. The existing exporter
produced the exact additive 78-line schema change; the final clean suite above
includes that contract check. Windows' initial default pytest temp path was
inaccessible; fresh explicit temporary paths worked without host permission changes.

Peer evidence notices were bus-accepted for Claude
`29264b0e-d4d8-4742-ac2e-3b2285fc8853` and Hermes
`f89ce9c0-69aa-48b1-9fef-1e01a13e794b`. Processing/wake is not verified. No
receipt-only response, deployment, activation or infrastructure work was requested.

### Inert native-event queue runtime

Tested source `c05557fdd00f8120e4a8690203b22785ba501e48`, development branch
only. Clean Git archive image index
`sha256:6a6385ba2e33ca562cd5541be15c608bdf6f3ea8d40d375df514cef155def298`,
manifest `sha256:913a965f58354d43d1886aa1b362530981aa627282fa94f1bd64628b2f76c2b1`,
config `sha256:5b27da3df3150a5ba597d69d4eedb655979b250dc0b0895b5375dd7460d0b0f9`.
Full exact-image Linux suite: **3174 passed, 3 skipped, 4 warnings**, 149.53s.
Warnings were the existing Starlette deprecation, an unclosed test event loop,
and two deliberate multithreaded-fork deprecations. Focused runtime, restore,
bridge, wire, queue and host-ledger checks: **98 passed**, 6.42s, with
ResourceWarning errors. Three ownership checks separately passed **3/3**,
0.30s, on the same image using only CHOWN and synthetic fixtures.

`QueuedPilotRuntime` is an explicitly started library, not an installed worker.
It subscribes before the initial drain, coalesces native events and runs one
eligible job at a time without a timer or inbox polling. Stop, source failure
and manager cancellation immediately veto new effect admission while accepted
work drains. Independent review accepted the runtime and bridge hooks. Thirteen
runtime tests cover lifecycle and cancellation; two additional tests reopen a
real independent host ledger with synthetic effects to check target-fence
regression and busy-target denial. They do not establish real host acceptance.

The initial working-tree focused run had **1 failed, 97 passed**: the idle test
read SQLite while measuring native callbacks and did not await quiescence.
The test now observes actual bridge drain calls and waits for native/runtime
quiescence; production code was not relaxed to resolve the test synchronization.

Claude accepted the single physical-machine reservation invariant and withdrew
the requirement to replace this bridge with an outbound pull worker. The next
source boundary is a restricted public gateway to the private database owner:
ordinary dashboard writes must not bypass `PilotOwner`, and the public process
must not receive the executor credential. This is not implemented or activated.
Machine-wide restore admission also remains closed: retained per-target host
fences alone do not prove a restored Hub can safely release physical capacity.
Independent control-generation reconciliation is still required before such
recovery can be accepted. No lifecycle factory, live service, credential, NAS
change, deployment or reboot acceptance is implied by this checkpoint.

### Queue-to-remote-pilot source integration

Implementation `33e0206c84a28c598eefbb53bdac6b05094b6220`; final tested source
`12f15aef8dbf414626968b857422b93c5e86401b`, development branch only. Clean Git
archive image index
`sha256:36d56fb30aee076bf112f9e858c83d4c84739505451427023caa41626dc36912`,
manifest `sha256:39380532be7fc788e5343eacb90fd3d0e42b29beb10aad2315bb56cad98e6600`,
config `sha256:50e7e3afd3beb69fe4d4bc7e1e6702a8b777d38340652c988dccb8fad1dbaa72`.
Full exact-image Linux suite: **3159 passed, 3 skipped, 4 known warnings**, 150.39s.
Focused bridge/wire/API/queue/recovery/capability/topic checks: **173 passed**,
8.06s with ResourceWarning errors and one existing Starlette warning. Three
ownership checks separately passed **3/3**, 0.31s on the same image.

Independent review accepted the explicit `QueuedContainerPilot` API, persistent
late-object/repository guards, disabled direct routes and per-phase admission.
Twenty-five new Linux-owner integration cases plus two wire/API cases verify
original intent/request/run/fence bindings, held machine exclusion through
restart, healthy rollback mapped to queue failure, policy revocation, private
worker admission, malformed proof chains and no effect replay. One wire case
uses the real client and `PilotASGI` with deliberately synthetic host assertions:
it loses an accepted smoke response, then GET-only recovery completes the same
run without another POST. These assertions are not real Engine/health evidence.

The first clean implementation run reported **2 failed, 3157 passed, 3 skipped**,
157.55s. Both failures were pre-existing topic tests whose module-collection
timestamp expired under the real 120-second evidence TTL before execution.
The test-only correction creates evidence when requested and compares one stable
snapshot for REST/MCP parity. The explicit one-day-old evidence test remains;
production freshness logic was not changed. Review accepted that narrow fix.

At this earlier checkpoint, no installed lifecycle factory/native worker loop, host credential, baseline,
runtime identity issuer, NAS operation, actual remote pilot or reboot acceptance
is added. Default application queue/pilot exclusion stays enforced. The deployment
transport was still being discussed: Claude proposed an outbound worker, requiring
new source APIs/receipt enrollment, while this bridge reuses the existing typed
host endpoint. Private credential isolation and concrete single-reservation
failure cases must be resolved before activation. The later runtime checkpoint
above records Claude's revised position and the remaining isolation gates. See
`remote-software-staging-bridge.md`; no new hardware or mandatory VM is required.

### Remote executor request concurrency

Implementation checkpoint: `e2997979529bb8db1182f1fbfe1f3e3328d210b6`, development
branch only. A clean Git archive built image index
`sha256:ab17ca58417b3ab097d29cf764e31cae7b3b4f44832bfde620a3965ed20767d7`,
manifest `sha256:41dd9f262aaec101bcc246624d6015b811cd96a9b2fb7a1ecf4fb89ef6dae1e7`,
config `sha256:82830924854acc2f09038a27a3483f099040dbc08cd936e5b59f27962127f785`.
The full exact-image Linux suite passed **3132 tests, 3 skipped, 4 known warnings**
in 148.82s. Focused ASGI/client/adapter/daemon tests passed **210/210**, 1.58s,
with ResourceWarning errors; three ownership checks separately passed **3/3**,
0.31s, on the same image. The earlier working-tree run passed 3132/3 skipped in
147.64s; the clean archive result is the authoritative committed-source result.
An initial ownership-check invocation omitted the test-data tmpfs and failed
during collection; the corrected isolated invocation passed. No test was
silently counted from that failed run.

Independent review accepted the bounded adapter offload. Eleven new real-thread
tests cover submit/get/reconcile responsiveness, busy admission without waiters,
body deadlines while work is active, repeated cancellation drain, context
propagation, scheduling failure, exception release, PID and cross-loop behavior.
The synchronous adapter no longer blocks the ASGI loop, but a cancelled client
does not stop accepted host work or release its gate early. Busy inspection is
unavailable rather than queued behind the same private ledger. The wire client
still treats a non-success response as unknown and never automatically resends.

This is isolated source/HTTPX/SQLite/thread evidence, not remote host or Docker
driver acceptance. No listener, private credential, Docker context, host mount,
NAS operation, old MCP endpoint/token/port, production deployment or reboot
configuration changed. The queue-to-pilot reservation bridge remains unimplemented;
see `remote-software-staging-bridge.md` for its concrete invariants and tests.
Current queue/pilot exclusion remains in force. Existing equipment and continued
source-only work supersede any requirement to provision a new PC or mandatory VM.

### Earlier archive HTTP checkpoint

Opt-in archive HTTP checkpoint: `2ff8f6d`. Clean Git archive image index
`sha256:804574caae4e73a3ee6902d16c570f587ad959b415aa5c181525409164e2c370`,
manifest `sha256:5fda5fa8d5a9b1106b7fa92aa444a2825ff71ef6496b05d52e5f6e3ec4484642`,
config `sha256:f4ba03c77fb6f9498ef2cbd25a2717949a85f20b407da2bf28b015ca2e08c682`.
Full exact-image Linux suite: **3073 passed, 3 skipped, 4 known warnings**,
145.33s; the three owner-change checks separately passed **3/3**, 0.31s.
Focused archive/auth/ingress/default-contract tests passed **261/261**, 6.51s,
with ResourceWarning errors and one existing Starlette warning. Independent
review accepted the HTTP projection and explicit factory boundary. Default
startup still adds no archive routes or schemas; no environment variable adopts
a bundle. Existing authentication supplies principals; selectors, duplicate
bearers, write verbs and private response fields are denied. Caller retains
reader shutdown ownership. This is ASGI/SQLite fixture evidence, not live data,
NAS setup, production sign-in or installed archive retrieval acceptance.

### Earlier archive reader checkpoint

Archive reader checkpoint: `fb2694c`, following reviewed archive integration
and synthetic kernel-mount acceptance. Clean Git archive image index
`sha256:c444022da7d69346a20e8fc329fd47ac6d900006ad8cf1c03a7108614c78c0a7`,
manifest `sha256:83f36dc9281d4ede8d8cbde759c006dfe99b36c99af373c580d50fe4e33da51e`,
config `sha256:16050420978709e98c910f6785aa78e002a408392c9c37e5c1eaca46002f1577`.
Full exact-image Linux suite: **3037 passed, 3 skipped, 4 known warnings**,
144.50s. The three owner-change checks separately passed **3/3**, 0.31s.
The source-only reader has 46 focused passing tests with ResourceWarning errors.
Independent review accepted its sender/recipient ACL and immutable read boundary.
Separate synthetic kernel checks verified read-only access, denied writable
mounts, and preserved private-message visibility (see `hub_archive/README.md`).
This adds no installed REST/MCP/UI route, actual archive adoption or replay;
actual coherent export, authenticated read-surface integration and original
data acceptance remain required. The full staging pilot gates above also remain.

### Earlier baseline observer checkpoint

Baseline observer checkpoint:
`5fa2e8494ad6f4c7fc9bb40319105d8650013892`, development branch only.
A clean Git archive produced image index
`sha256:259c0153e8d5074b1253185e1dd8efa84267ffde781725cea39b664fd5de7fec`,
manifest `sha256:1c441cfbfa0dcce7d7a863bbc6311697a270214410a3b5c4514d83555a425104`,
config `sha256:f925239eb1737188c2e3d42cb0ddd43399665e112679b1befb386faefb4ab81b`.
Full exact-image Linux suite: **2961 passed, 3 skipped, 4 known warnings**,
138.33s. The three owner-change checks separately passed **3/3**, 0.28s,
on the same image. Focused observer/snapshot/receipt/profile tests passed
**100/100**, 47.65s, with ResourceWarning errors enabled in the working image.

Independent source review accepted the one-attempt observer after mock-transport
and frozen-inode descriptor safeguards were added. Observation checks runner,
image, profile, unchanged original inventory, frozen SQLite integrity and exact
terminal private probe receipts before returning draft bytes. Tests use actual
private SQLite journals and separate Linux processes with a quiet WAL writer,
but **simulate Engine methods**. They do not establish real daemon/container
health. Receipt publication, independent operator pinning, trusted driver-runtime
identity and actual deployment/rollback/reboot acceptance remain incomplete.
No live host, NAS, endpoint, credential or installed worker changed.

## Earlier baseline preparation checkpoint

Baseline preparation checkpoint:
`372f38b96a81970188c770869afb02aa2a18ff38`, pushed to the development branch,
not main or an installed image. Clean Git archive image index
`sha256:84de0f6abaa98e85859af7485fc4e624eadc8c3c6152062e50ac6d1b483eb02a`,
manifest `sha256:5d35c83aafa0a16631760bd98081cfd8c66f357278028ab2783fffd4486a328f`,
config `sha256:2a98b268773e477d3e21f8e6200eb210fb19edbaf84bfde940e272b3af2306b2`.
Full exact-image Linux verification: **2936 passed, 3 skipped, 4 known warnings**,
102.63s. The three owner-change checks passed separately **3/3**, 0.29s.

This adds a protected historical baseline receipt reader, bound file inventory,
shared pure runtime-profile derivation and an explicit first-baseline snapshot
helper. Actual Linux SQLite/WAL snapshot tests use a separate operator process
while the fixture writer remains running; database/WAL/SHM bytes and identity
remain unchanged. The helper rejects an already-open source descriptor or a
multithreaded operator and never normalizes the live source. An initial test
measurement itself released the writer's POSIX locks; moving that measurement
into another process fixed the test without relaxing production guards.

Receipt-reader integration exercises real private storage and candidate/rollback
state copies with **synthetic health authority**. It is not actual application
health, observed baseline enrollment, driver-runtime identity, real container
deployment/rollback or reboot acceptance. The actual health enroller and
independent pinning workflow are still missing; the acceptance gate stays
disabled. No live host, NAS, MCP endpoint, token or production state changed.

## Earlier private executor lifecycle checkpoint

Private lifecycle implementation checkpoint:
`dc9d1dffd71a4cc658aa56d242613aaa44fe500b`, pushed to
`work/hub-pilot-integration-codex`, not main or an installed image.
A clean Git archive produced image index
`sha256:28ce50baeb55f71be8b594cd2b7e1c19cb799f0bdcab7508de48b121852146bb`,
manifest `sha256:e7690bdeba9715375de84c46f134d0e8e7faf90260e2372becd1bba56ea5d265`,
config `sha256:5d0c4ac47f5b0c85739ea538cd22ef2a0ed8b5cd39017b76e272528e01db5cfa`.
The exact image passed **2821 tests, 3 skipped, 4 known warnings**, 90.31s.
The three owner-change checks passed separately **3/3**, 0.29s, with only CHOWN.
The final focused identity, authentication, ownership, runtime and composition
set passed **92/92**, 2.06s, with ResourceWarning errors enabled. Independent
source review found no blocker.

This adds actual Linux lock/native-feed lifecycle verification with a **mocked
execution backend**, not an installed remote executor or a new real Docker pilot.
It proves startup draining, event-driven invalidation, accepted-work shutdown,
fresh restart identity, exact private admission and agent-token impersonation
denial. No entrypoint, credential or worker service was activated. Existing
shared staging Viewer/PA resources remain outside the allowed pilot boundary;
see the shared-host activation gate in `staging-validation-worker.md`.

An earlier working-tree image passed 2860 tests; that count includes unrelated
untracked tests and is not the clean-commit release evidence above. The exact
clean commit is the authoritative checkpoint.

## Earlier queue and actual local Engine checkpoint

Final implementation checkpoint:
`2132e8727e9eeb3688a59075ddd3eb11f20ea282` on the same development branch.
A fresh clean Git archive was built as image index
`sha256:a4bfe1acfcc881ffe45be3357a73f3c18fc1e122a7eda3f86a2295e18bc1d6aa`,
selected manifest
`sha256:73c6ff2e63858f887af18fea834f2560dbb2bf87a549aa0fb7122ba312543c8c`,
config `sha256:79bd865ef8e3da2eb248a5b4990d03354ff4b6e5c9efa7f80e60b4244253bf16`.

The exact final image passed **2701 Linux tests**, with 3 owner-change skips
separately verified **3/3** (0.31s), and 3 known warnings, in 93.12s. The full
suite includes opt-in MCP transport, closed bootstrap admission, exact worker/
target authorization and action-filter tests. The actual-HTTP/native-SSE smoke
also passed against this image. The actual local Engine acceptance was repeated
successfully against this exact image with explicit test-only worker admission:
fixed tests succeeded, the separate exit-7 command failed correctly, forged
evidence was denied, receipts reopened, reservations released, and only the two
receipt-bound stopped containers were removed. Production remained blocked.

The older a377 checkpoint below preserves the initial acceptance/fix history;
it is not the final source. All remote, deployment, credential and reboot gaps
below remain. Existing approved SSH access to staging `192.168.50.90` was
verified read-only, including Docker availability; no host configuration or
service was changed. Staging root reported 19 GiB free of 98 GiB, so image/scratch
retention must be bounded before any installed worker is activated.

Beau selected software workflows first. Bulk ODM/OpenSplat/TreeDAR processing
workers remain separate future work. This is **not** a report that the remote
staging installation is ready.

## Exact source/artifact verification

Reviewed source checkpoint: `a377e26e68140bf9abc5dd598914721bd535e3ba`, on
`work/hub-pilot-integration-codex`, not main. A clean `git archive` of that commit
was used as the complete Docker build context, excluding unrelated untracked
work and local datasets. No new production image was published or installed.

- Local validation image index:
  `sha256:402c3aa41620d043232e46d197e10f2f79b39c912408b63beb26ccfe5420890b`.
- Selected linux/amd64 manifest:
  `sha256:4feb828b8d7ee53232dffd5829742a47eb89a9f253422b105fac65d0a8cbdf8d`.
- Image config:
  `sha256:8d60fc69c9537cf92954d428dbf26bb7cd34aa2b0f3e3591e35520ae4f6aa914`.
- Full exact-image Linux suite: **2648 passed, 3 skipped, 3 warnings**, 93.48s.
  The skipped owner-change tests separately passed **3/3**, 0.31s, against the
  same image with only CHOWN and synthetic fixtures. Known warnings: Starlette,
  an unclosed asyncio test event loop, and deliberate multithreaded fork.
- Actual HTTP coordination smoke on that image passed real 401 handling,
  non-preemptive production priority with synthetic approval, machine exclusion,
  instance fencing, expiry hold and exact native SSE queue invalidation. This
  test's coordination-only actions did not execute anything.

## Actual local Engine validation

An explicit Windows driver connected to the existing local Docker Desktop Linux
Engine, using its pinned daemon identity and the preloaded exact image above.
The original local OCI archive bytes established the index/selected manifest/
config chain. Task reviews and operator enrollment were **synthetic fixtures**,
not a verified CI attestation or human production approval. No real token or
live Hub database was used. No host Docker socket was mounted into a container.

The first actual test invocation exited 4 because the test conftest creates a
data directory next to the read-only test tree. The backend correctly recorded a
verified failed receipt and released that job; it did not claim success. The
exact original stopped container was inspected and removed without force. Its
small synthetic intent/receipt databases were retained.

The driver was corrected to copy the fixed test tree into disposable `/tmp`
before invoking the fixed tests. Runtime isolation was not relaxed. The corrected
actual local acceptance passed:

- Fixed queue/API/bootstrap tests executed and exited zero.
- A separate fixed command exited 7 and produced a failed result, not success.
- Original image, effective sandbox profile, exit and quiescence were inspected.
- Trusted receipts survived reopening; a fabricated evidence ID was rejected.
- Both verified terminal jobs released their machine reservations.
- Unapproved production stayed queued; no deployment run or task promotion.
- Only the two exact receipt-bound, independently verified stopped containers
  were removed without force. A final local listing found none remaining.

The worker used **Windows test-only receipt storage**. Linux permissions were
tested separately in the exact-image suite; the Windows run does not establish
Linux private storage, remote staging, trusted launcher identity, reboot recovery,
application deployment/rollback or production execution acceptance.

## Remaining first-use gap

The new HTTP queue and local validation worker are not yet an authenticated
Hub-to-staging-PC execution service. Do not place the Hub database on SMB or let
agent assertions create trusted receipts. The remote adapter, its bounded
execution/receipt contract, private provisioning and installed lifecycle still
need implementation and actual-host acceptance. The default production verifier
and executable completion/reconciliation provider remain disabled.

After peer review, the configured bootstrap was further hardened to deny claims
entirely until a trusted server-side exact-worker/action/machine/target policy
is installed. Merely enabling the queue cannot wedge the machine by reserving
an unexecutable job. Optional action filters only narrow that policy. The local
acceptance harness explicitly enrolls its precise synthetic target and bound
receipt provider; this is not remote executor credential provisioning.

The staged agent API base remains `http://192.168.50.80:8770/api/hub/v1/work`,
and the Hub MCP listener is `http://192.168.50.80:8770/mcp/`; these new queue
capabilities are **candidate source**, not live installed endpoints. The old MCP
on 8765 is unchanged. Beau retains TrueNAS management/installation ownership.
