# Existing-equipment Hub pilot: preparation proposal

Status: **preparation approved; not installed or activated**. Beau approved
preparing a disposable, resource-limited pilot on the existing staging PC and
asked to see the exact setup before activation. This does not authorize changing
Viewer, PA, production, TrueNAS, current MCP, credentials or Access policies.

Fresh read-only host evidence and a proposed bootstrap-only approval are in
[`staging-bootstrap-proposal-2026-10-19.md`](staging-bootstrap-proposal-2026-10-19.md).
That proposal records earlier observations, not current installed-pilot readiness.
The dedicated account and subordinate-ID allocation now exist, but actual
container mappings, independent issuer installation and the full-pilot resource
budget remain unverified. Neither the setup script nor the prepared source is a
drop-in activation command. See the current read-only handoff below.

## Scope and ownership

- Hermes: inspect existing host writers and prepare the host-specific boundary
  proposal. Request `ea5aee38-63cd-4c7c-a832-db0198c9012d` carries the approval.
- Codex: prepare the private coordinator/executor composition and exact immutable
  release handoff; retain the existing physical-machine queue authority.
- Claude: independently review acceptance and visible readiness. Request
  `d7ce5baa-415e-4b44-bf01-0ec9ab30d2fb` carries that review.
- Beau: approve the final host changes and activation after the exact artifact,
  resource budget, known risks and rollback/stop procedure are presented.

Messages accepted by the bus are not proof that a peer is awake or processing.
Do not launch replacement sessions or a polling listener to complete this plan.

## Proposed first test, not a production migration

Use `staging` (`192.168.50.90`), not TrueNAS or the Hermes machine. Keep
`192.168.50.80:8765`, the live Hub on 8770/8771, its database and tunnel untouched.
The target is a new disposable **Engineering Hub** instance, not Viewer, PA,
ODM/OpenSplat/TreeDAR, or the coordinator performing its own replacement.

The following are proposed logical names, not claims that host paths or services
already exist. Hermes must resolve them against the actual operator account,
available storage and existing watcher selectors before any creation:

| Component | Proposed identity and boundary |
| --- | --- |
| Whole pilot | `hub-pilot-eh-first`; a fresh run UUID distinguishes each attempt |
| Physical machine | Existing canonical `staging-pc`, never a per-container alias |
| Private coordinator | One local persistent pilot-only Hub DB and `PilotOwner`; no SMB DB and no second dispatch queue |
| Public gateway | Separate process with read-only coordinator UDS only; no DB, executor secret or Docker socket |
| Host executor | Dedicated private process using `PilotASGI`/`HostAdapter`; process UID/GID 10001:10001 in its current user namespace required by the state provisioner; only it can access the approved test daemon and private receipt/intent state |
| Disposable application | Manifest prefix `hub-pilot-eh-first`; UID/GID 10001, read-only root, no capabilities, no new privileges, no host PID/IPC namespace |
| Application networking | `network_mode=none`, no published ports; fixed probes execute inside the exact owned container |
| Application data | Separate baseline/candidate generation directories, local to the approved executor; no PA/Viewer/NAS/application-data mounts |
| Private controller/executor transport | Host-local authenticated UDS preferred; exact endpoint, peer protection and secret resolver still to be supplied |
| Persistence | Private owner/intent/receipt state remains durable across process restart; disposable application data is not a substitute for that state |

Use the dedicated rootless test boundary on the same PC only after the actual
host supports the required isolation/cgroup features and resource accounting.
No new PC or VM is required. Bootstrap account/unit preparation is not proof of
an active, accepted container runtime; the dedicated runtime is currently stopped.
Do not install or enable anything merely to satisfy this document. Do not fall back to
the existing shared rootful socket without a separately reviewed and approved
authority/writer boundary. A second daemon still shares physical resources and
does not itself coordinate Viewer workers, PA cron or host cleanup.

The current source's executor process UID/GID 10001 checks and private mode-0700
state directories need a real compatibility check against rootless UID/GID
remapping. Those process checks apply in the executor's current user namespace;
they do not independently prove initial-host namespace identity. A container's
numeric UID 10001 is not necessarily host UID 10001. Do not change
ownership, weaken private modes or claim bind-mount access merely because the
numbers look the same. This is an unresolved compatibility question, not a
rootless installation recommendation. [Docker rootless architecture](https://docs.docker.com/engine/security/rootless/)
documents the user namespace; mapped identities and actual storage access must
be verified in the chosen executor/daemon/container topology.

Rootless resource flags require cgroup v2 and systemd, and can otherwise be
ignored. Verify delegated controllers and effective cgroup files before claiming
limits. [Docker resource-limit guidance](https://docs.docker.com/engine/security/rootless/tips/#limiting-resources)
is the reference, not a passing Compose parse. Host root remains privileged
over both daemon environments; separate contexts only establish the tested
ordinary authority boundary. The rootless install guide's suggestion to disable
the system Docker service is **not applicable here**: existing apps must remain.

## Resource proposal to verify before activation

Start with one workflow at a time and no image builds or dataset processing on
staging. Prepare immutable images elsewhere, then explicitly approve any transfer.
Final image sizes and free-space headroom must be measured, not inferred from
Prometheus's earlier sample.

For each disposable Hub application generation, propose **512 MiB memory with
Docker `MemorySwap=Memory` (512 MiB combined, no additional swap), 500 millicores,
128 PIDs and 64 MiB `/tmp` tmpfs**. These
fit the existing closed manifest bounds; they are not yet tested sizing for the
actual release. `/tmp` counts toward the memory ceiling. Exact effective Engine
inspection must match the reviewed manifest, including `restart=no` and image
default command/entrypoint. Increasing limits is a reviewed profile change.

The implementation must enumerate its maximum simultaneously live generations,
including failure/rollback, and budget the controller, executor, gateway, daemon,
image unpacking and filesystem cache as well. Per-container caps do not prove a
whole-pilot resource reservation. Do not approve activation using only the
application's 512 MiB cap or assume PA's currently unbounded containers are idle.
No directory-size promise is an enforced disk quota. State/image/scratch bounds
and minimum remaining free space need an actual enforcement/admission plan.

## Exact inputs still required in the installation artifact

1. Selected test daemon socket and observed daemon/platform identity. The shared
   daemon ID from the inventory is evidence of existing workloads, not an
   automatic choice for the new executor.
2. Approved operator-owned paths for private controller state, receipts, local
   generations, UDS and test-only secret files; exact UID/mode/ancestor checks.
   Never reuse or copy the live MCP tokens/database into a disposable instance.
3. Exact candidate and rollback source commits and runnable OCI digest chains,
   their build records and independent review. A local Docker config ID alone
   is not a runnable image reference or a provenance approval.
4. Independently verified driver/launcher identity and build/baseline enrollment,
   plus a healthy disposable baseline and coherent immutable backup. Do not use
   test permissive callbacks or make an approval by hashing an untrusted record.
5. Explicit composition selecting `serve_owned_queued_pilot`, a dedicated
   `ContainerPilotClient`, protected executor transport, and mandatory exact
   target policy. The shipped ordinary `create_app` entrypoint does not do this.
   The ordinary Hub image still omits the root-level executor package. Separate
   optional wheel packaging, concrete Unix launch-receipt transport, host-root
   supervisor and fixed installed-runner module now exist; source landed in
   [PR7](https://github.com/ledgetoptechnologies/Engineering-Hub/pull/7).
   The optional consumer-image recipe and wheel verification landed in
   [PR8](https://github.com/ledgetoptechnologies/Engineering-Hub/pull/8)
   (main `59ee49c`), not as a change to the deployed Hub entrypoint.
   The explicit `local_host_adapter_resources` context now joins the real backend
   to the existing protected provider implementations. It consumes already
   approved inputs. The installed runner obtains a direct signed launch receipt
   before constructing mutable providers. The concrete supervisor checks actual
   Engine peer/process/profile/mapping evidence and issues that receipt from an
   independently protected key/enrollment. These implementations do not install
   that host authority, supply its approval, or prove the real host measurements.
   Explicit queued-coordinator and local-UDS client source wiring now exist;
   [installation and readiness](installed-pilot-coordinator.md) remain unproved.
   No default ordinary Hub command selects this pilot.
   A dry configuration validator must remain effect-free; serving/enrollment
   must be separate explicit operations. Rootful Docker capability remains
   host-level authority despite UID 10001 and limited target containers.
   The subsequent [owned executor service seam](staging-executor-service.md)
   supplies the listener/resource lifecycle and explicit local provider assembly.
   It does not supply the missing
   trusted installation/enrollment configuration or activate the host.
6. Writer selectors and application job-state evidence: image watcher, cleanup,
   Viewer worker, PA cron and any manual/CI deployer. Specify coordination or
   exclusion before the first effect; do not stop existing services implicitly.
7. Installed supervisor ownership, stop/drain budget, endpoint-generation
   recovery, and current-host/restore reconciliation. Expired leases, an old
   process exit or a restored SQL file do not establish safe machine release.
   Current source requires at least **1120 seconds** of supervisor grace;
   propose **1200 seconds**, then verify the installed supervisor actually
   honors it. This is not permission to kill accepted work after that time.

These become a concrete, independently reviewed installation artifact. Until
resolved, this document is not a Compose file or runnable installation command.

## Current read-only operator handoff

The October 19 read-only SSH snapshot found `ehpilot` UID/GID 10001, private
mode 0700 home `/var/lib/eh-pilot`, and separate subordinate UID/GID allocations
`200000:65536`. The user manager and slice were inactive, there were no UID 10001
processes, and `/run/user/10001/eh-pilot-docker.sock` was absent. These are a
point-in-time observation, not permission to start it or proof that other host
writers are absent. The configured `user-10001.slice` ceiling is **1 GiB memory,
no swap, one CPU-equivalent and 256 tasks**; while inactive, it supplies no actual
daemon/container cgroup-enforcement evidence. Shared root-filesystem available
space was **13,901,606,912 bytes** (approximately 13.9 GB, 87% used), not a reserved
pilot allocation or enforced disk quota.

The root-owned installed bootstrap unit still has `RuntimeMaxSec=600`,
`TimeoutStopSec=1200`, `Restart=no` and `SendSIGKILL=no`. Its ten-minute lifetime
is not a full-pilot lifecycle and must be reconciled with the source's minimum
**1120-second** drain grace. A timeout does not establish quiescence or authorize
forcing an unknown operation terminal.

The local optional image's runtime manifest
`sha256:53581ae334b5bc10860a43a5d39ed623655ad964d27b6f5583d8a7b2275e9a75`
has verified OCI descriptor/layer hashes and installed-runner/image-default
checks. It used the independently recorded **local** core artifact, not the
published core digest default in the optional recipe. This is local artifact
evidence, not release enrollment, source attestation, host installation, direct
issuer acceptance or a completed application pilot.

The smallest remaining operator decisions and prerequisites are:

1. Approve an aggregate budget and bounded lifecycle for the exact setup. The
   original baseline, candidate and newly restored rollback can all remain live;
   current adapter behavior does not first stop the failed candidate. At the
   proposed 512 MiB/128 PID per-generation profile, **three generations alone total
   1.5 GiB and 384 PIDs**, plus daemon, executor, controller/gateway, cache and
   scratch overhead. This is incompatible with the existing 1 GiB/256-task bootstrap
   ceiling as a guaranteed budget. Per-container caps and sequential phases do
   not fix that mismatch. Do not silently raise limits or weaken profiles.
2. Separately approve and install the host-root issuer's immutable code and
   dependencies, independent root-private signing key, closed enrollment,
   durable journal and new protected socket parent. Keep them outside daemon-
   readable mounts. Library/image inclusion of issuer code grants no such
   authority. No key generation, root installation or activation is authorized
   by this document.
3. Measure actual daemon and container UID/GID maps, supplementary groups,
   namespaces, mount access and effective cgroup limits before provisioning
   mapped-owner files. Allocation arithmetic is not an observed mapping.
   Expected issuer `SO_PEERCRED` UID/GID are interpreted in the consumer user
   namespace; the issuer process's peer GID is **not** the socket file's access
   group. Neither may be guessed from matching numeric labels.
4. Independently provision and enroll disjoint private stores, exact config
   bytes, same-absolute-path generations, immutable build records, distinct TEST
   credentials and a healthy disposable baseline/frozen backup. Retain physical-
   machine queue ownership and exclusion of image watchers, cleanup, Viewer,
   PA cron and other deployers; do not borrow live MCP credentials or data.
5. Select the private controller connection explicitly. Local-UDS coordinator
   source wiring does not establish installed reachability. A staging-local pilot does not demonstrate
   TrueNAS-to-staging remote access: the Unix-only executor has no installed
   authenticated remote forwarder. Any later TLS/SSH-UDS gateway and private-path
   access require their own exact reviewed topology, not broader NAS/root access.
6. Present that concrete artifact, host change set, stop/drain procedure and
   retained-evidence plan for approval before activation. Actual direct-PID1
   receipt, resource enforcement, validate/deploy/health/rollback and restart
   reconciliation remain acceptance gates. The launcher preserves uncertain
   outcomes; it does not automatically adopt or relaunch held work after restart.

## Activation and acceptance sequence to present to Beau

1. Record the existing objects/configuration and exact proposed additions. Show
   expected storage, resources, endpoints, credentials and which changes require
   installation. Check for concurrent work immediately before execution.
2. Obtain approval for that exact setup, not a blanket production or cleanup
   permission. Only then create owned pilot resources and establish the healthy
   disposable baseline. No public DNS/tunnel/Access change is part of this test.
3. Create a real pilot task and require a different agent to review the exact
   source. Submit the immutable target through the actual coordinator queue;
   prove native delivery, claim/fence ownership and physical-machine exclusion.
4. Deploy the candidate using the real backend and verify immutable identity,
   effective isolation, fixed app/assets/auth-denial probes and persistent result.
   A service's self-reported health commit alone is insufficient.
5. Deliberately exercise an independently specified failing candidate and verify
   a new rollback generation restored from the frozen baseline backup and its
   health, while preserving the original baseline. Record actual
   outcomes; do not turn an ambiguous effect into a passing rollback assertion.
   The current adapter creates a distinct rollback container from restored
   baseline state; verify its exact journal-bound identity rather than require
   it to have the original baseline container ID. Verify the preserved original
   baseline independently remains unchanged.
6. Exercise process interruption/restart without replaying uncertain commands;
   retain held work until original-identity quiescence is authoritatively known.
   App/process restart is not PC reboot or power-loss acceptance.
7. Check Viewer, PA, old MCP and live Hub remain unchanged. Produce the observed
   artifact/job/run/evidence IDs and remaining installed human-UI/reboot gates.

## Stop and cleanup contract

The initial installation must not auto-start at boot until explicitly approved
and tested. An operator stop first closes new admission, then drains accepted
effects under the configured budget. Unknown outcomes keep their physical-machine
reservation and private journal; never forcibly mark them complete to stop.

Cleanup must verify exact resource IDs, run identity, namespace and current
ownership before deleting anything. Preserve mismatches or uncertain resources.
Do not use global prune, wildcard deletion, `compose down --volumes` against an
existing project, or delete private evidence during rollback. The final plan
must list the exact owned resources it will remove and the evidence it retains.
