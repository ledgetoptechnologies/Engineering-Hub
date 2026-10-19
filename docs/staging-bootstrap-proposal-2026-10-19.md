# Existing staging PC: bounded rootless bootstrap proposal

Status: **earlier bootstrap approved and partially installed; clean-environment
revision not installed or activated**. This records the original Codex read-only
observations and the subsequent Hermes checkpoint, not an instruction to run the
existing installer, activate an executor or deploy an application. Hermes remains
the existing host-operation owner; source review does not reassign that work.

Hermes reported in message `b4fb8c7b-bcb0-4149-a436-891a8d79932a` that it
installed the approved uidmap/libsubid4 prerequisites, locked UID/GID 10001
`ehpilot` enrollment and three original unit files. It checked the manager's
resource ceilings, then stopped **before Docker launch** because global SSH/GPG
socket activation and inherited `SSH_AUTH_SOCK` were not part of the reviewed
environment. The report says the owned manager/runtime were terminal, runtime
directory absent and shared daemon/21 containers/15 volumes unchanged. This is
peer-reported partial bootstrap evidence, not a successful daemon/pilot test or
current independent host observation. Retain the exact enrollment for review.

## Original pre-installation observations, not current readiness

Read-only SSH used the existing `bkoltz@192.168.50.90` connection, BatchMode and
strict existing host-key verification. No sudo, package installation, daemon
start/stop, account/permission change, config edit, image operation, secret file
read or processing/application job was performed.

| Observation on 2026-10-19 | Meaning and limitation |
| --- | --- |
| Host `staging`; bkoltz UID/GID 1000, with Docker and sudo groups | Existing operator identity is privileged; do not use it as the isolated daemon owner |
| Shared rootful dockerd PID 1042, UID/GID 0; daemon ID `4a562438-a128-4837-acb6-f85fd207ebee` | Existing Viewer/PA daemon remains untouched; PID is an observation, not durable identity |
| Engine reports systemd cgroup driver and cgroup v2 | Existing rootful Engine configuration, not a rootless enforcement test |
| Existing user@1000 manager active; cpu/memory/pids delegated and enabled in subtree control | Useful prerequisite evidence for this user only; a new account's actual delegation still needs verification |
| Rootless setup tool, rootlesskit and slirp4netns present | Files found, not proof that rootless Docker is installed/running |
| `uidmap` package, `/usr/bin/newuidmap` and `/usr/bin/newgidmap` absent | Concrete missing prerequisite, independently checked with dpkg and file metadata |
| No `/run/user/1000/docker.sock`; process-name snapshot found only rootful dockerd | No rootless daemon found in these checks, not exhaustive process/socket absence proof |
| `/etc/subuid` and `/etc/subgid` each allocate only 100000:65536 | Observed range belongs to bkoltz; must not reuse it for another account |
| No passwd or group record returned for numeric 10001 | Proposed new service UID/GID appears available; not reserved, recheck before allocation |
| Root filesystem ext4; 14,599,258,112 bytes available | About 13.6 GiB at this sample, not a quota or approved space budget for image imports |
| MemAvailable 5,445,540 KiB at one sample | About 5.2 GiB; not a memory reservation or proof other workloads will stay idle |
| Unprivileged user namespaces enabled; AppArmor user-namespace restriction enabled and rootlesskit profile present | Real unprivileged launch compatibility still needs testing; no sysctl/AppArmor change is authorized |

The nightly `staging-docker-cleanup.timer` is enabled/waiting. At observation,
its service was inactive/dead, last run 2026-10-19 04:32 CDT, with next timer
2026-10-20 04:43 CDT. Inactivity is not exclusion of future cleanup.
The unit has no reported drop-ins and explicitly sets the system Docker Unix
socket plus `DOCKER_CONTEXT=default`. A bounded AST-only inspection of its
8,283-byte Python script found Docker image inspection/removal and Buildx prune
forms. It did **not** execute that script or print arbitrary environment values.
The EnvironmentFiles property was not emitted, so this inspection does not
establish its absence. The full subprocess environment/selection flow was not
audited. Revalidate the effective endpoint before claiming cleanup cannot touch
new test state; do not stop, modify or run the existing cleanup to make the test pass.

## Proposed first authorization: bootstrap only

The first request is deliberately separate from application-pilot activation.
Its purpose is to establish whether the existing PC supports a dedicated bounded
test daemon without changing the current Docker/Viewer/PA/Hub/MCP/TrueNAS setup.

| Proposed addition | Scope requiring approval |
| --- | --- |
| OS prerequisites | Install `uidmap` and `libsubid4` only, after repeating a no-upgrade simulation and reviewing the exact package plan |
| Linux service account | New `ehpilot`, proposed UID/GID 10001, locked/non-login, no SSH keys/password, no Docker/sudo membership or copied operator credentials |
| Subordinate IDs | Proposed 200000:65536 in both files, only after rechecking all allocations and relevant real-account conflicts immediately before allocation |
| Private local home/data | Proposed `/var/lib/eh-pilot` and `/var/lib/eh-pilot/docker-data`, dedicated owner and private permissions; no SMB or existing application mounts |
| Runtime endpoint | Proposed `/run/user/10001/eh-pilot-docker.sock`, local Unix socket under that account's private runtime directory; no TCP listener or published port |
| Runtime service | A reviewed **user-systemd** rootless Docker unit for that account, manually started for this test, not enabled; no linger or boot autostart |
| Aggregate bootstrap cap | Proposed new-account parent cgroup: 1 GiB memory, one CPU-equivalent, 256 tasks, covering daemon and descendants; actual placement and enforcement must be checked |
| Delegation | Narrow new-account-only cpu/memory/pids delegation if required; no global change to existing users or services |

The package simulation—not installation—used the current local APT cache and
reported `uidmap` plus `libsubid4`, both candidate version
`1:4.13+dfsg1-4ubuntu3.2`, zero upgrades/removals and 64 packages not upgraded.
It also printed unrelated autoremove suggestions; **do not run autoremove**.
Simulation has no locking and is not a reservation or guarantee that a later
package plan will match. Do not run apt update/upgrade or accept a changed plan
under this proposal without the appropriate approval.

This bootstrap permits only daemon prerequisites, owned private paths/unit and
read-only identity/resource checks. It permits **no image pull/import/build,
application/processing job, Hub executor/issuer activation, signing-key creation,
token reuse, public endpoint, DNS/tunnel/Access change or reboot**. If tiny
container fixtures are later needed to prove UID/cgroup enforcement, present
their exact immutable image, resource budget and operations separately first.
No new queue/capacity registration is part of bootstrap. The physical machine
remains the canonical `staging-pc`; a separate daemon must not become an alias
that bypasses machine-wide production-first scheduling or outside-writer checks.

Do not run the installed setup tool's default `install` operation unchanged: its
source calls user-systemd start and enable. An enabled unit is not the approved
manual-only bootstrap. A separately reviewed unit/launcher plan must keep boot
registration disabled and avoid changing bkoltz's Docker context. Docker's docs
also say a system-wide rootless service is unsupported even with `User=`; use
the dedicated user manager, not an improvised system-wide Docker service.
[Docker rootless prerequisites](https://docs.docker.com/engine/security/rootless/)
and [rootless service/resource guidance](https://docs.docker.com/engine/security/rootless/tips/)
support these requirements. Do not follow the rootful-daemon shutdown example
on this shared staging PC.

## Checks before calling bootstrap successful

Exact user-unit/parent-slice/delegation files and post-approval user-runtime,
manager, start and stop operations are in
[`deployment/staging-bootstrap`](../deployment/staging-bootstrap/README.md).
The earlier versions were subsequently reported installed as described above;
the revised environment-whitelist unit has not been installed. Before that
installation, the target's actual systemd 255 parser accepted
all three configurations with exit zero and no output, using anonymous in-memory
files and aliases; delegation was checked merged with its existing user@ template.
Generators/man-page checks were disabled. No files were copied to the target,
units registered, managers started or conditions/runtime limits exercised.
`dbus-user-session` and `libpam-systemd` were also observed installed.
Thirteen local static contract tests passed. These check the proposed source
configuration, not an installed daemon, effective manager environment or kernel
enforcement. RuntimeMaxSec requests a stop; with forced killing disabled, it does
not guarantee termination at the deadline. Preserve and inspect nonterminal state.

1. Record the exact approved package/account/range/path/unit additions and verify
   each is fresh or matches a deliberately reviewed prior enrollment. Fail on a
   collision; do not adopt, chmod or delete an unknown path.
2. Recheck current memory/disk headroom, concurrent work and cleanup endpoint;
   no existing service is stopped or workload assumed idle.
3. Verify the dedicated owner has no inherited privileged groups or operator
   credentials, and the daemon uses only its explicit new Unix socket/data root.
4. Verify rootless security mode, new daemon ID/platform, actual current namespace
   mappings and cgroup parent/controller limits. Available flags are not proof
   that rootless resource limits are enforced. This image/container-free bootstrap
   can prove placement/readback, not workload stress behavior; tiny runtime fixtures
   need the separate approval described above.
5. Verify no TCP/published endpoint, autostart/linger, default-context switch,
   image/application job or old-object modification was introduced.
6. Stop only the newly owned daemon/user unit after the bounded check, preserving
   its state for inspection. Bootstrap success is not permission to leave an
   unattended worker running or to activate the application pilot.

If bootstrap fails, retain bounded diagnostics and private state. Do not globally
prune, remove existing containers/volumes, uninstall dependencies automatically,
delete an account/home recursively or stop the shared daemon. Any cleanup must
first verify exact ownership and account for new concurrent use.

## What still separates this from a real pilot

The source's UID/GID 10001 checks apply in the executor's **current** user
namespace. A rootless container UID 10001 can map to a different host UID.
Do not change existing providers or modes to bypass that fact. Actual executor
and daemon bind-path visibility/ownership must be tested in the chosen topology.

The proposed 1 GiB bootstrap cap is **not** the full-pilot budget. Three possible
application generations at the existing 512 MiB/500-millicore/128-PID/64-MiB-tmpfs
profile total 1.5 GiB memory, 1.5 CPU-equivalents, 384 PIDs and 192 MiB tmpfs;
tmpfs already counts toward memory. Add and measure controller, gateway, executor,
daemon, image unpack/cache and state/journal overhead under a verified aggregate
limit. Separate containers/daemons do not reserve the physical machine away from
Viewer/PA or other writers.

Exact immutable image sizes, unpack peaks and state retention must fit an agreed
disk admission floor. That floor is not an enforced disk quota. The real pilot
also requires an independently packaged executor, authenticated launcher/issuer,
actual process/container correlation, independent build/baseline enrollment,
outside-writer coordination and Beau's activation approval. Installed human UI
and PC-reboot acceptance remain separate. No bootstrap result satisfies them.

See [the pilot preparation proposal](staging-pilot-preparation-2026-10-19.md) and
[launch identity contract](executor-launch-identity.md).
