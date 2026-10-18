# Operator-pinned Docker Engine backend source

This file documents `daemon_backend.py` as source-only. The implementation was
exercised only with `httpx.MockTransport`, captured fake Engine transcripts,
synthetic OCI archives, and private temporary generation directories. It has not
connected to Docker, opened a listener, created a container, or changed host
permissions.

## Binding and request surface

`DockerEngineBackend` requires an explicit `DockerEngineBinding`: enrolled runner
ID, expected daemon ID, shared platform, absolute Unix socket path, exact API
version, and enrolled generation root. Construction does not connect. Real
requests use HTTPX's Unix-domain-socket transport, a fixed `http://docker` base,
`trust_env=False`, and the exact configured `/v<api_version>/...` path. It does
not inspect environment variables, discover a socket/context, call `/version`,
negotiate an API version, accept a remote URL, or fall back to another socket.
Only explicit `httpx.MockTransport` injection is accepted as a test transport.

The pinned API version must use canonical numeric `1.<minor>` spelling and be
at least the shared private-cgroup feature floor, API 1.41. Unsupported versions
are rejected while validating the binding, before transport construction or I/O.
Higher operator-pinned versions remain unchanged; there is no negotiation or
downgrade. The closed profile requires explicit `cgroupns_mode="private"`, sends
`HostConfig.CgroupnsMode="private"`, and accepts effective readback only when
that exact field is present and private. Missing, default/empty or host mode is
not treated as evidence of namespace isolation.

The runner check reads `/info`, compares daemon `ID`, OS and architecture, and
returns the operator-enrolled runner ID and platform. Docker's architecture
aliases are accepted only for `amd64`/`x86_64` and `arm64`/`aarch64`. The Engine
`/info` response alone does not prove an ARM CPU variant; the variant remains
operator-pinned and must also be proven by original image metadata.

## Image identity and supported archive path

The only create reference is `VerifiedImage.runnable_reference`, which must be
an immutable SHA-256 runnable index/manifest digest. The original config digest
is never submitted as the runnable image. No tag, pull, build, image mutation,
or fallback is attempted. Image admission reads both:

- `GET /images/<runnable-digest>/json` for a known chain member and defaults;
- `GET /images/<runnable-digest>/get` for a bounded archive, parsed without
  extracting paths or materializing layer payloads.

The accepted archive format is a strict OCI layout whose top descriptor matches
the immutable runnable digest and whose original descriptor bytes prove the
unique `index -> selected platform manifest -> original config` chain. Hashes,
media types, descriptor sizes, platform/variant, duplicate keys, nesting, body
size, archive paths and metadata budgets are checked. The daemon inspect `Id`
may be the runnable index, platform manifest, or original config ID, but only
because the independently read archive binds those values into one chain. The
image inspect defaults must match the original config defaults.

**Unsupported/held:** classic Docker-save archives that do not retain the
original index-to-platform-manifest chain; missing/truncated/oversized archives;
unknown inspect IDs; extra/ambiguous descriptors; wrong platform/config; and
Docker responses that do not expose the strict shapes described above. There is
no reserialized-config or label substitute. For a multi-platform ARM64 index,
an actual container whose `.Image` is only the root index ID is unsupported;
its actual selected platform is not established. A platform-manifest or config
ID from the verified chain is required. For AMD64, an index-only `.Image` is
accepted only alongside the pinned daemon architecture and the archive's unique
AMD64 selection.

This is metadata identity evidence only. Layer bytes, build provenance,
attestation issuer, and driver implementation identity remain external; the
backend does not authenticate layers or claim provenance. The adapter still
requires its separate `TrustedDriverIdentityVerifier` backed by a trusted issuer.
The backend cannot satisfy that verifier by echoing an ID or label.

### Concrete runtime enrollment still required

Do not fill the missing runtime verifier with a constant copied from the pilot
manifest or package metadata. The preferred acceptance boundary is a separately
built, non-editable executor artifact and an independently trusted launcher that
checks the actual launched image/config/platform, command, read-only filesystem,
mounts and process instance. Mutable checkout hashing is development evidence,
not proof of an immutable installed runtime.

A private signed launch-receipt design was independently reviewed as viable,
but is not implemented or accepted here. Its launcher must authenticate the
requesting process (for example, kernel Unix peer credentials), map that live
process to the real container and bind a fresh provider-generated nonce to its
container/process-start/boot identity. Inspecting a caller-supplied victim PID
or container is not sufficient. A provider would require a separately pinned
verification key and artifact enrollment, strict bounded canonical fields,
dedicated protocol/audience, one-shot receipt acceptance, fork denial and bounded
issuance/expiry converted to a fixed monotonic lifetime. No request-selected key,
algorithm, expected identity or refresh may become authority.

Such evidence establishes launch conditions for a bounded interval, not memory
attestation or cancellation of effects already admitted. The actual authenticated
launcher and process/container correlation must be exercised with the Engine API
before claiming the real pilot passed. This note does not activate a launcher,
expose a socket, generate production credentials or authorize host changes.

## Closed create profile

Before the create POST, the backend rechecks runner and image metadata, validates
the exact typed profile and generation source, and refuses an existing resource
name. Creation uses a digest-only image and preserves image-default entrypoint
and command by omitting overrides. It sets UID/GID `10001:10001`, read-only root,
`CapDrop=ALL`, `no-new-privileges`, non-privileged mode, private PID/IPC
(default namespaces, with host/container modes omitted), network `none`, no
published ports, bounded memory/swap/CPU/PIDs, restart `no`, and one state bind
plus the bounded `/tmp` tmpfs (`noexec,nosuid`, mode `1777`). Environment is the
verified image defaults plus only fixed Hub paths and the three explicit
manifest-backed test-secret values. Secret values are not included in log/error
messages, evidence, labels, or journal calls.

The state source is derived only as `<operator-enrolled-generation-root>/<opaque
state_generation_id>` from the trusted adapter plan. The root and generation
must be owned by the current UID, not group/other writable, non-symlink
absolute directories; the generation tree rejects symlinks and special files.
No request-supplied path, additional mount, socket mount, or host path is
accepted. The separate state provider must still establish UID/GID 10001 access
and generation/baseline integrity; the driver does not provision or chown state.

After create, the backend reads the actual container inspection and fails closed
unless the actual selected image chain, command defaults, environment, state
source, tmpfs, ports, namespace modes, resource limits, user, capabilities,
restart policy, and security settings match. An inspect mismatch after a
successful create is an unknown outcome; there is no start/retry.

## Lifecycle, probes, and reconciliation

`start_container` and `stop_container` require a known exact-profile container,
recheck `/info` against the pinned daemon immediately before and after the
mutation request, then inspect and verify resulting state. Each exec create/start
request in the compiled-probe path is likewise preceded by a pinned-daemon check;
response loss or a post-effect identity mismatch remains unknown. These separate
requests still cannot eliminate a local socket-path replacement race against a
privileged operator; that control remains part of the operator enrollment gate.
There are no arbitrary commands, exec options, environment overrides, or
container IDs accepted from the wire.

Only `run_compiled_probes` uses Docker Exec. It accepts exactly the shared three
ordered `Probe` schemas and fixed per-probe budgets. The fixed Python script uses
`/usr/local/bin/python3 -I -c`, no shell, no caller code, and only GETs
`http://127.0.0.1:8770` inside the exact container. It checks the Hub health JSON,
HTML title and bounded same-app assets, and unauthenticated 401 response. It
removes the three test tokens from the exec process environment and never sends
them in HTTP headers. Docker's multiplexed output is bounded and parsed without
stderr disclosure. Exec creation, start, output, and exit status must all
complete; timeout or response loss is unknown, not proof of cancellation. Inspect
and reconcile methods never exec, start, or probe.

`inspect_operation` does not infer terminal outcomes from missing containers,
labels, daemon errors, or inspect state. Without an explicit trusted private
operation journal it returns `unknown`. With one, the backend writes only
operation ID/kind and daemon-acknowledged completion facts; it never passes
request bodies or secrets. Journal integrity, privacy and durability are an
operator provider gate, not supplied here. A failed write after a daemon effect
remains unknown. No terminal failure is synthesized from absence or HTTP error.

Calls use one monotonic budget per typed backend call, recheck around metadata
and journal admission, pass the remaining timeout to requests, and check before
subsequent effects. Bounded response streaming checks the deadline per chunk.
The synchronous HTTP transport/daemon operation is not cancellable by that
check: after a timeout or response loss the result stays unknown and the backend
never retries. The adapter's durable operation intent remains authoritative.

## Verification and remaining gates

Focused fake-only command:

```sh
PYTHONPATH=.:src /home/bkoltz/.hermes/cache/scratch/internal-MCP-messaging/.venv/bin/python \
  -W default -m pytest -q tests/test_hub_container_executor_daemon_backend.py
```

Passing fake transcripts are unit evidence only. The exact Docker Engine API
response/archive/export semantics, containerd `.Image` behavior, mount/tmpfs
inspect representation, and attached Exec stream behavior remain **unverified
against a real daemon** in this phase. An independently reviewed, separately
authorized local acceptance must establish those semantics before activation;
unsupported responses must remain unavailable rather than relaxing these checks.

Still external: trusted build-record/content stores and verifier, secret resolver
for test-only slots, private state/generation provider and baseline proof,
independent driver implementation issuer, and a durable trusted private operation
journal. No test here establishes actual daemon identity, effective isolation,
UID/GID state access, health, rollback, layers, provenance, or host-project
acceptance. The acceptance harness remains outside this task and was not run.

## Review corrections and unresolved profile gates

The tmpfs `Mounts[].Mode` may be the documented empty string; security options
remain mandatory in `HostConfig.Tmpfs`. Nonempty mount modes are additionally
validated, and a missing `noexec` or `nosuid` is still rejected. Fake create and
readback fixtures now use the documented empty mode, not an invented repetition
of options. This is not an actual-daemon compatibility result.

Generation regular files must have exactly one hard link. A hard link to an
outside inode is rejected before any create POST. This walk is not an atomic
filesystem confinement mechanism: the enrolled state provider must exclude
concurrent generation renames/tree changes through validation and daemon bind
establishment. No lease or race-free provider is supplied here; that remains an
activation gate, especially when an outside same-UID writer could mutate paths.

The shared manifest and `EffectiveProfile` currently model private PID/IPC
namespaces, **not cgroup namespace mode**. This backend neither pins nor verifies
`CgroupnsMode`; it must not be described as guaranteeing a private cgroup
namespace. A host-default cgroup namespace can currently pass that unmodeled
property. Shared-model ownership remains with Codex: an explicit profile decision,
compatible API minimum, source implementation and inspect tests are required
before claiming private cgroup isolation or approving an operator enrollment
that requires it. This conditional review finding is not silently treated as
fixed or as permission to activate.
