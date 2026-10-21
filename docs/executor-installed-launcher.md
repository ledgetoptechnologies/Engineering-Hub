# Concrete executor launch and receipt composition

Status: **optional source implementation, not general installed readiness**.
Importing these modules installs nothing and launches no service or container.
No credentials, accounts, mounts, privileges or production MCP configuration are
provisioned by this work. The separately documented
[bounded TEST pilot](staging-pilot-acceptance-2026-10-21.md) used an independently
enrolled artifact and the native API, with actual queue/rollback/shutdown evidence.
That does not install the new reusable CLI below, establish a general installer,
or prove live NAS dispatch or reboot recovery.

## Explicit source APIs

| Component | Contract |
| --- | --- |
| `launch_supervisor.LaunchSupervisor` | Initial-host-root operator API taking explicit `enrollment_file`, `signing_key_file`, `journal_file` and `create_journal`. `launch()` performs one enrolled container create/start; `serve_one_receipt()` serves one direct authenticated init-process exchange. No installer, background worker or automatic recovery is supplied. |
| `launcher_cli` | Explicit installed host-root command composing that existing API, one receipt exchange and a bounded wait for an explicit stop signal. No provisioning, implicit launch, force/retry, service registration or reboot recovery. |
| `launch_transport.UnixLaunchReceiptTransport` | Consumer-owned synchronous transport taking explicit `socket_path`, `expected_issuer_uid` and `expected_issuer_gid`. `issue_receipt(nonce=..., timeout_ms=...)` makes one direct connection with no discovery, relay, TCP or retry. |
| `installed_runner` | Fixed direct-init entry point `/opt/venv/bin/python -I -m hub_container_executor.installed_runner --config /operator/runner.json`. `load_configuration()` loads the protected read-only configuration; `run_installed_executor()` obtains verified launch identity before assembling providers and serving the private executor API. |
| `installed_coordinator` | Separate opt-in colocated TEST pilot. `validate --config <path>` reads protected configuration and validates its data only; `serve --config <path>` resolves TEST credentials and selects the existing owned queued-pilot lifecycle through a fresh private Unix client. It does not replace the deployed unified MCP. |

The supervisor consumes independent root-protected release/image/profile/daemon
enrollment and an existing raw Ed25519 private key. It does not generate approval
or keys. Its enrollment schema is `executor_launch_supervisor_v2`; v1 is rejected
without fallback or implicit migration. The runner's
separate closed configuration schema is `engineering-hub.installed-executor.v1`.
Canonical JSON and matching hashes validate shape and consistency, not approval.
The configuration's exact bytes are pinned by the required top-level root
enrollment field `configuration_raw_digest`, outside the launch profile hash.

### Acyclic configuration commitment (v2)

The runner embeds its release's `launch_profile_digest`, while the launch profile
includes the config mount. Hashing those complete raw runner bytes directly into
that profile would require a cryptographic fixed point. V2 uses a finite DAG:

1. Build a fully valid canonical runner template with a provisional valid-format
   `release_enrollment.launch_profile_digest` (for example, a zero SHA-256).
2. `configuration_commitment.configuration_commitment(raw)` first applies the
   complete strict installed-runner parser. It then replaces **only** that exact
   digest leaf with the internal fixed marker
   `engineering-hub.executor-profile.self-reference.v2`.
3. Compute `C = SHA256(domain || canonical(normalized_runner))`, with domain
   `engineering-hub.executor-configuration-commitment.v2` followed by a NUL byte.
   The config mount's `content_digest` is `sha256:<C>` in v2; it is **not** the raw
   configuration SHA-256. All other profile fields and mounts remain hashed.
4. Hash that entire profile to obtain `D`, then serialize final canonical runner
   bytes with the embedded digest set to `D`. Normalizing the final runner must
   reproduce the same `C` without a fixed-point search.
5. Compute the exact final raw configuration SHA-256. Independently enroll it as
   required root field `configuration_raw_digest`, together with profile `D`,
   release/image/daemon pins and the remaining root policy.

Each verified config-file read checks the **same bytes** against the independent
raw SHA-256, normalized `C`, complete independently enrolled release identity and
`D == profile.digest()`. Missing or malformed raw pins, legacy schema, duplicate
keys, noncanonical JSON and unknown fields fail closed. Changing only embedded
`D` leaves `C` unchanged but fails independent release equality; changing another
runner field changes `C`. Updating a raw pin alone cannot approve either change.
The raw pin is never optional and normalized commitment is never autodetected.

The existing signed receipt v1 protocol is unchanged: its signed `D` commits the
full profile containing `C`; embedded `D` is separately bound to independent root
policy. Existing root-file, source/mount, kernel peer, PID, nonce, lifetime,
issuance-journal and uncertain-effect safeguards remain required. This source
repair requires a newly built/reviewed executor artifact and explicit enrollment;
it does not relabel an older image, migrate a journal or establish host readiness.

## Identity and direct exchange

The supervisor authenticates the independently pinned Engine Unix-socket peer
and retains daemon/process identities. The daemon must share the issuer's PID
namespace so its reported container `State.Pid` is interpretable there; another
daemon PID namespace fails unsupported. The executor itself uses a private PID
namespace. Its retained init PID, pidfd liveness, host boot/start identity,
effective container profile, mounts and installed runner bytes are checked before
issuance. A caller cannot nominate a victim PID or existing container. An init
wrapper or a forked receipt relay does not satisfy the direct-init peer contract.

The issuer creates and pins its new socket, binds that file into the executor,
and requires the accepted kernel peer to be the retained live init process with
the measured host UID/GID. Its mode `0660` group is the independently measured
consumer host GID. The signing key, enrollment and issuance journal stay outside
executor mounts in protected root-owned storage. A key readable by the rootless
daemon UID would not be independent: that daemon's Engine authority can expose
files accessible to that UID through container mounts.

A read-only bind of the Engine socket does **not** make Docker API operations
read-only. The executor retains control of that dedicated daemon. It must never
receive the staging machine's shared or production Docker socket.

On the client, issuer UID/GID pins are interpreted in the **consumer's user
namespace**. Initial-host root can appear as an overflow identity; peer PID can
be zero when the issuer is outside the consumer PID namespace. Neither is a
reason to assume host PID equality or blindly pin UID zero. Peer credentials and
stable leaf device/inode checks do not replace the independently pinned Ed25519
key, trusted mount composition or issuer-side consumer authentication.

Each direction is one unsigned four-byte big-endian length followed by canonical
UTF-8 JSON and write-side EOF. The request is at most 256 bytes and contains only
`protocol=engineering-hub.executor-launch-request.v1` and a 64-character lowercase
hex nonce. The response is the existing signed receipt envelope, at most 8192
bytes. The client half-closes after sending; the issuer rejects trailing request
data before reserving issuance. The client requires EOF after its response under
the same whole-exchange monotonic deadline. Fragmentation does not renew it.
See [receipt identity verification](executor-launch-identity.md) for exact signed
fields, enrollment comparisons and lifetime rules.

## Configuration and mount mapping

All sources are independently provisioned and enrolled. The closed launch profile
requires these roles; only `state` and `generations` are writable:

| Mount role | Executor destination / runner configuration |
| --- | --- |
| `engine_socket` | `/run/engineering-hub/docker.sock`; `docker_binding.unix_socket_path` |
| `issuer_socket` | `/run/engineering-hub/issuer.sock`; `launch_transport.socket_path` |
| `config` | `/operator/runner.json`; v2 normalized commitment in profile plus independently enrolled exact raw configuration SHA-256 |
| `state` | `/var/lib/engineering-hub/executor`; separately provisioned ledger, journal and reservation paths beneath it |
| `generations` | **Source equals destination at the exact host absolute path**; `docker_binding.generation_root` |
| `records`, `metadata`, `baseline`, `secrets` | `/var/lib/engineering-hub/records`, `/var/lib/engineering-hub/metadata`, `/var/lib/engineering-hub/baseline`, `/var/lib/engineering-hub/secrets`; corresponding explicit resource paths |

The generation path is not an alias such as a container-only `/generations`.
Docker resolves application bind sources in its host filesystem namespace; the
executor must write and reference the same enrolled absolute generation path.
Each resource's existing disjoint-parent and private ownership checks still apply.
The frozen backup and baseline receipts require explicit provisioned paths and
their existing content/ownership checks; no missing path is created or repaired.

The runner requires Linux PID 1 and UID/GID 10001 in its own user namespace. It
reads one regular, single-link mode `0600` configuration owned by that UID from a
read-only mount, with no symlink path and stable metadata. Its configuration
contains the manifest, exact daemon binding, provider paths, independent build
and release enrollments, public key, launch transport pins, receipt budgets,
executor socket and separate versioned TEST bearer binding. It contains no
private signing key or literal secret values. Application TEST slots and the
executor bearer slot must be distinct. Parsing does not prove these inputs were
independently enrolled.

## Reusable explicit host launcher

Use the independently verified optional wheel in an approved **host** Python
environment, not by overriding the consumer container's PID1 entrypoint. The
wheel preparation, wheel validator and offline image import probe require the
`launcher_cli` module; presence in an artifact is not permission to run it.

The operator supplies existing independently protected enrollment and signing
key files, an existing root-private local journal parent, and a **fresh** journal
path. There are no campaign-specific PID, inode or daemon constants in this CLI;
the native supervisor validates the independently enrolled current identities.
It does not generate those enrollments from observations or create their parents.

```text
python -I -B -m hub_container_executor.launcher_cli \
  --enrollment-file <existing-protected-enrollment> \
  --signing-key-file <existing-protected-key> \
  --journal-file <fresh-root-private-journal-path> \
  --wait-seconds 1200 --execute --create-journal
```

Without `--execute`, valid arguments return `not_executed`: no native supervisor
is constructed and no key, enrollment, Engine or journal is opened. This is **not**
an installation/readiness validator. Execution additionally requires Linux,
host root, the native kernel facilities, main-thread signal handling and explicit
fresh-journal consent. Native construction can create the journal and its lock;
an interruption before launch may therefore leave them present. Never delete,
reset, reuse or adopt retained journal/lock/socket evidence to retry this command.

The same process performs one `launch()`, waits for its own consumer's receipt
request, performs one `serve_one_receipt()`, then waits for an explicit stop.
Readiness waits at most 60 seconds, clipped by the existing receipt expiry,
watching only the retained listener and exact consumer pidfd. Consumer exit or
readiness timeout preserves held evidence, without retry or automatic stop.
Readiness does not authenticate a pending connection: the unchanged one-shot
exchange still checks its peer, profile and nonce within its original request
deadline (at most 10 seconds). Cold startup therefore does not consume that
exchange deadline before the consumer has connected.

`SIGUSR1`, `SIGTERM` and `SIGINT` only set a flag; native operations
are never invoked from a signal handler or a second thread. A stop during the
synchronous exchange is considered only after that exchange succeeds. A request
after launch or during readiness skips issuance and requests one stop.
No failed native operation is retried or converted into automatic cleanup.

**Drain the coordinator and all admitted jobs before requesting launcher stop.**
The native supervisor's stop deadline is at most 120 seconds; it is not the
services' at-least-1120-second busy-work grace allowance. The CLI does not prove
drain. Signal only the retained, independently verified owning launcher process;
do not select a process by a guessed PID, name, or newest-session lookup.

`--wait-seconds` accepts 1–3600 and bounds **only the wait after issuance**. It
does not extend receipt authority or bound all native operations together. Expiry
returns nonzero and closes descriptors only; it does not terminate a container.
Schedule the explicit owned stop before that wait expires. A native error also
returns nonzero without force, removal, adoption, replacement or POST retry.
Treat retained state as requiring reconciliation, not as successful shutdown.

Output uses fixed non-secret phase names; `stopped`/exit 0 is emitted only after
the one native graceful stop and descriptor/handler cleanup succeed. Source/mock
tests exercise ordering, cancellation boundaries, failure/expiry, restoration
and argument validation. The first 2026-10-21 isolated CLI attempt exposed the
missing readiness wait: it failed before issuance and its consumer exited
nonzero; its held journal remains preserved, not adopted or retried.

A separately installed fresh TEST generation later exercised this corrected
CLI with the actual Linux kernel, protected host runtime and pinned consumer.
The receipt exchange succeeded. Exact Engine/kernel/profile/namespace/cgroup
checks and two GET-only authentication probes passed. One retained-launcher
pidfd `SIGUSR1` requested graceful stop; the original launcher handle returned
0. An independent closed-state audit verified native `stopped`/issuance `sent`,
consumer exit 0/PID 0/non-OOM, removed sockets, zero application-command and
operation rows, and unchanged ID/state/image rows for all 16 pre-existing TEST
containers. No application job, coordinator, TLS proxy or boot autostart was
started in this acceptance. See the scoped
[fresh CLI evidence](staging-cli-acceptance-2026-10-21.md).

That installed TEST result is not permanent-service, NAS-route, live Hub
activation or reboot recovery acceptance. Source tests alone do not establish
those properties either. The earlier application rollback pilot and this
GET-only CLI acceptance are separate acceptance runs. This fresh executor
generation reuses historical campaign5550 application artifacts without
executing its rollout; it is not a new application campaign.

## Lifecycle and evidence limits

Launch/start and issuance are journaled without automatic retry or adoption.
An uncertain effect remains held. `graceful_stop()` is an explicit owned SIGTERM
operation; timeout does not authorize forced killing, deleting or launching a
replacement. `close()` closes descriptors and preserves live/unknown container
and socket evidence; it does not establish quiescence. Transport `close()` revokes
future use without cancelling an already admitted exchange.

The runner retains ownership of the verifier while the owned service drains
admitted work and closes providers, then revokes it. This does not extend its
receipt lifetime. Receipt expiry blocks future identity
checks; it neither cancels already admitted effects nor proves their completion.
The service remains Unix-only with its independently resolved TEST bearer; no
remote forwarder, unattended startup or reboot recovery is installed.

Linux source tests exercise real private Unix sockets, framing, signatures,
deadline/closure/fork cases and runner composition. Supervisor fixtures model
Engine/process/profile behavior; they do not deploy Docker or prove actual PID
mapping, cgroups, immutable image installation, peer attribution, graceful host
shutdown or a successful application pilot. No synthetic signer is an enrolled
production issuer. Python isolated mode and read-only code are not memory
attestation or protection from trusted root/same-UID Engine administrators.

Continue through [staging pilot preparation](staging-pilot-preparation-2026-10-19.md)
only with the actual independently packaged release, enrollment, host readiness
and ownership evidence. The ordinary Hub image still does not install this
executor package; a validation image's passing tests do not close that gap.
