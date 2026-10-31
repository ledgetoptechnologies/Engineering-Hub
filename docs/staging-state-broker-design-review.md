# Dedicated staging state broker: implementation design for review

Status: design only. No source implementation, host provisioning, credential or access
change, live daemon acceptance, or deployment is implied. This is a proposed
replacement for the current mutable host-path bind boundary, not an assertion
that the existing pilot is isolated against same-UID writers.

## Problem and concrete target

The existing source does not establish separate host identities for
`FrozenSQLiteStateProvisioner` and the rootless Engine; their actual host UID
mapping must be verified during enrollment. The executor receives both
`engine_socket` and the generation path through `ExecutorLaunchSupervisor`, and
`DockerEngineBackend._resolve_generation` walks a pathname before the Engine
binds it. A cooperating lock cannot prevent a different same-UID process from
renaming that path, editing its tree, accessing the daemon socket, or mounting
the same data elsewhere. Engine named volumes alone do not fix those authority
paths. `DAEMON_SOURCE.md` records the existing race as an activation gate.

Target one dedicated rootless Engine and one dedicated broker per pilot host.
The executor can issue a narrow typed request to the broker but cannot open the
Engine socket, read its data root, or access broker-owned generation storage.
The broker is the sole authorized Engine client and the sole state publisher;
the host administrator remains trusted. The app is the sole writer of its
assigned generation after start. The broker and Engine need distinct host
identities from the executor and app-mapped UID; use kernel-enforced access
controls and verified user-namespace maps, not process conventions. If these
properties cannot be established, deny startup.

## Source changes and interfaces

1. Add an explicitly versioned broker mode to the signed launch profile and
   installed runner configuration. Keep the current profile and bind behavior
   unchanged for existing configurations; neither mode implies rollout approval.
   In `launch_supervisor.py`, a broker-mode profile must reject an `engine_socket`
   or `generations` mount and instead mount only a broker UDS. Pin broker socket
   inode, owner/mode, peer credentials and a root-protected enrollment containing
   the broker executable/image identity, daemon identity and expected UIDs/GIDs.
   Extend the profile parser, signed profile digest, installed coordinator and
   launch tests together; no fallback to direct socket when the broker is absent.
2. Add a separately launched broker service with a narrowly scoped UDS protocol.
   `SO_PEERCRED` plus a pinned pidfd/start identity and independent launcher
   enrollment identify the executor; pathname permissions alone are insufficient.
   Authenticate the broker to the executor similarly and pin the socket identity
   before and after connection. The broker, not the executor, opens the dedicated
   Engine socket. Its root-owned configuration pins Engine ID, API version,
   daemon process identity, socket inode, user maps and private storage roots.
   Deny ambiguous peers, changed pins, forks, unsupported API versions, malformed
   frames and inherited authority. No generic HTTP proxy, path forwarding, CLI,
   arbitrary Engine payload, shell, image pull, host mount or arbitrary exec.
3. Implement a `PilotBackend` client for the broker and wire it explicitly in
   `installed_runner.py` and `local_resources.py`. Reuse the exact manifest,
   verified image and runtime-profile derivation; the broker independently
   validates enrolled manifest/image/configuration commitments and constructs
   the closed Docker request. The wire surface must only support runner/image
   inspection, creation of an enrolled candidate or rollback generation,
   fixed-profile create/start/stop, fixed compiled probes and original-operation
   inspection. Every effect carries its original command/run/fence/operation ID.
   Read-only inspection and replay return bounded typed results. Existing direct
   backend remains the old explicit mode, never selected as a fallback.
4. Replace the broker-mode state provider with a broker-owned, exclusive
   generation lifecycle. Consume an independently enrolled frozen SQLite backup;
   copy it into a fresh Engine-managed local volume using a fixed, verified
   provisioner image and a fixed command, or use an equivalent broker-private
   filesystem mechanism if the actual user map makes that safer. Validate exact
   volume driver, options, daemon ID, ownership, volume name and app UID mapping;
   reject external drivers, bind-backed volume options, preexisting names,
   duplicate mounts and unexpected attachments. Independently read back the
   volume identity and effective container mount. Never mount the volume or
   expose its backing path to the executor. SQLite integrity, backup digest,
   baseline inventory and resource admission remain mandatory. A named volume
   is a storage primitive, not evidence of isolation by itself.
5. Maintain a broker-owned durable journal on a distinct protected local
   filesystem. Record canonical manifest, run/fence, state, operation, exact
   profile/image/generation commitments and phase intent before each effect;
   record exact daemon responses afterward. Bind replies to the original request
   and broker epoch. An authenticated ledger receipt alone does not replace the
   broker journal; socket labels or current container state do not prove history.
   Interrupted intents are held, never repeated. On restart, reacquire exclusive
   broker/daemon ownership, recheck enrollment and reconcile only original IDs.
   Keep the adapter's authenticated create context and its existing held behavior.
6. Define a broker admission/fence boundary that covers *all* authorized daemon
   and generation writers, not merely Python locks. At minimum the daemon socket
   and backing storage must be inaccessible to the executor and other untrusted
   host users, and only the broker may create, mount or mutate pilot generations.
   Hold the broker's single-owner fence through publication, create and receipt
   commit. Before a held command can advance, additionally define and implement
   a current-observation/commit protocol that prevents competing authorized
   changes and treats lost ownership or uncertain post-commit state as held. A
   check followed by SQLite commit is not atomic with daemon state; no success
   transition is enabled until this contract has been reviewed and tested.

## Limits, failure and rollback

Use one active campaign and one in-flight effect; reject excess requests rather
than queueing unbounded work. Set fixed request and response byte limits, archive
and frame bounds, connection and whole-command deadlines, cgroup CPU/memory/PID
caps, storage quotas and reserved rollback headroom. Concrete values must be
pinned to the actual pilot enrollment and no greater than the reviewed manifest
limits; measure aggregate broker, Engine and app overhead before launch. Disk
admission alone is not a runtime quota. Budget deadlines never imply cancellation
or permission to replay an effect.

If a request loses its response, peer identity changes, the broker or Engine
exits, volume inventory differs, a journal write fails, or the broker cannot
prove no late work remains, stop admission and retain the original intent and
all owned evidence. Do not adopt a matching name or delete unknown containers or
volumes. Shutdown must stop admission, drain known work, close owned sessions and
verify owned process exit. Restart is explicit and read-only until evidence is
reconciled. Use the separately enrolled rollback image and reserved generation;
rollback is a distinct durable phase with its own fixed probes. Cleanup requires
a separately designed owner-checked retirement operation and must not delete
foreign or ambiguous objects. No health claim comes from a create/start receipt.

## Verification plan

- Unit tests: strict configuration and protocol parsing; duplicate fields,
  malformed MACs/receipts, wrong identity, wrong Engine, wrong namespace maps,
  missing socket pins, unsafe volume drivers/options and oversized inputs all
  fail closed before effects. Check default direct mode remains byte-compatible.
- Integration on disposable local Linux infrastructure: real UDS peer and
  pidfd checks, dedicated identities and user namespaces, exact Engine API
  transcript, mounted volume readback and storage access-denial tests from the
  executor/app identity. Attempt direct raw socket and backing-path access as
  each untrusted identity. An endpoint allowlist test alone is insufficient.
- Crash matrix: kill broker before/after each durable intent, Engine POST,
  response, receipt and state publication; restart with same enrollment and
  assert no repeated effect or adoption. Inject changed socket inode, daemon
  identity, generation, container and fence between inspection and commit;
  assert hold rather than success. Include client cancellation, bounded drain,
  quota exhaustion, rollback failure, and cleanup ownership checks.
- Separate reviewed acceptance: use pinned synthetic images and credentials on
  a disposable dedicated daemon. Prove effective UID maps, exclusive volume
  access, candidate fixed probes, controlled failure, rollback and rollback
  probes, reboot/restart behavior and cleanup of only test-owned resources.
  Fake transports and unit tests are not daemon or host acceptance evidence.

## Host-specific facts still required

Identify and reverify the actual staging host, owner and active workloads; the
installed OS/kernel/systemd and Engine versions; actual rootless daemon and
consumer UID/GID maps, subordinate-ID allocations and disjointness; available
service identities, permissions, socket and volume/data-root locations; exact
filesystem and quota support; SELinux/AppArmor and user-namespace enforcement;
local disk/memory/CPU headroom and recovery limits; existing daemon clients;
image and provisioner provenance; baseline and test-secret enrollment; and the
operator responsible for broker, daemon and state lifecycle. Observations in old
notes are historical, not proof of current ownership or access. Validate the
Engine's actual volume and inspect semantics on that host before accepting them.

Source changes can be developed and tested with synthetic inputs now. Creating
accounts, changing UIDs/groups, permissions, mounts, systemd services, socket
access, storage or quotas, enrolling keys/images/secrets, and starting or
reconfiguring the staging daemon are user-managed host/access changes outside
this design artifact. Review exact resulting configuration and ownership before
any such action; this document does not authorize it.
