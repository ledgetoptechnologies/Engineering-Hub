# Concrete executor launch and receipt composition

Status: **source implementation, not installed readiness or pilot acceptance**.
Importing these modules installs nothing and launches no service or container.
No credentials, accounts, mounts, privileges or production MCP configuration are
provisioned by this work. An independently built, enrolled immutable executor
image and actual host validation remain missing.

## Explicit source APIs

| Component | Contract |
| --- | --- |
| `launch_supervisor.LaunchSupervisor` | Initial-host-root operator API taking explicit `enrollment_file`, `signing_key_file`, `journal_file` and `create_journal`. `launch()` performs one enrolled container create/start; `serve_one_receipt()` serves one direct authenticated init-process exchange. No installer, background worker or automatic recovery is supplied. |
| `launch_transport.UnixLaunchReceiptTransport` | Consumer-owned synchronous transport taking explicit `socket_path`, `expected_issuer_uid` and `expected_issuer_gid`. `issue_receipt(nonce=..., timeout_ms=...)` makes one direct connection with no discovery, relay, TCP or retry. |
| `installed_runner` | Fixed direct-init entry point `/opt/venv/bin/python -I -m hub_container_executor.installed_runner --config /operator/runner.json`. `load_configuration()` loads the protected read-only configuration; `run_installed_executor()` obtains verified launch identity before assembling providers and serving the private executor API. |
| `installed_coordinator` | Separate opt-in colocated TEST pilot. `validate --config <path>` reads protected configuration and validates its data only; `serve --config <path>` resolves TEST credentials and selects the existing owned queued-pilot lifecycle through a fresh private Unix client. It does not replace the deployed unified MCP. |

The supervisor consumes independent root-protected release/image/profile/daemon
enrollment and an existing raw Ed25519 private key. It does not generate approval
or keys. Its enrollment schema is `executor_launch_supervisor_v1`; the runner's
separate closed configuration schema is `engineering-hub.installed-executor.v1`.
Canonical JSON and matching hashes validate shape and consistency, not approval.
The configuration's exact bytes are pinned by the enrolled config mount digest.

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
| `config` | `/operator/runner.json`; exact enrolled configuration-file bytes |
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
