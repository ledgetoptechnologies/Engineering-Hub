# Container host adapter: build-record contract V1

Pre-implementation response to assignment29a3187a-d6b1-4778-bef9-d16876c802fd. Base52d534ca4999012700eda6accf8dff71acfe8383. This freezes consumption of the shared PilotManifest and pilot request/response models at that source; it does NOT fork their schema, activate an adapter or claim an actual app rollout.

Reviewed container_pilot_manifest.py, the published response/profile contract and the metadata helper. Focused shared-manifest/client/workflow/API run:298passed, one existing deprecation,6.76s, exit0. Root's reported full1051 Linux results and actual Windows metadata-chain probe are separate peer evidence, not reproduced here. No Docker operation, listener, permission, credential or deployed-config change was performed for this source review.

## Shared profile agreement

Use PilotManifest.from_bytes/canonical_bytes/digest and ContainerPilotRequest/Response/Evidence directly. Keep exact four phases, original candidate commit on every phase, target+fence binding one run/manifest/provenance/policy, required response echo/replayed and existing reason vocabulary. No request-provided image, socket/context, URL, env, command, mount or daemon option. Current fixed network-none/no-host-port profile supports isolated application lifecycle/HTTP-inside-container only, not externally usable ingress. Tmpfs wire mode1777 is forwarded as Docker option digits mode=1777; it is not decimal chmod1777.

No widening of that profile is proposed. Before a driver can have effects, its configured runner/daemon/implementation identity, trusted provenance verifier, private state provisioner and versioned test-only secret resolver must be available. Missing authority/proof fails closed; a typed model is not that authority.

## Minimal immutable build record

The operator supplies a private content-addressed record store and immutable metadata-blob store. The caller request contains neither record bytes nor a storage path. For each manifest candidate/rollback, its existing provenance.attestation_digest selects the SHA256 of the following canonical record bytes. No added PilotManifest fields are required for this private record mapping.

All keys below are required and nested objects are closed. No URLs/paths/tags or credential values occur in the record. Types SHA/Commit/Platform use the same strict lexical/platform rules as the shared manifest.

```
BuildRecord {
  schema_version: literal "hub_container_build_record_v1",
  builder_id: bounded operator-enrolled opaque identifier,
  invocation_id: canonical UUID,
  source: {
    repository: literal "https://github.com/ledgetoptechnologies/Engineering-Hub",
    commit: lowercase40hex,
    build_context_digest: SHA256,
    build_recipe_digest: SHA256,
    build_inputs_digest: SHA256
  },
  platform: shared Platform {os,architecture,variant},
  selection: {
    runnable_reference: SHA256,
    runnable_media_type: OCI index | Docker manifest-list | OCI image-manifest | Docker schema2 image-manifest,
    platform_manifest_digest: SHA256,
    image_config_id: SHA256
  },
  metadata_blobs: [
    {digest: SHA256, media_type: supported index/manifest/config media type, size_bytes: strict bounded integer}
  ]
}
```

Exact builder_id bound/pattern is shared Opaque (1..128 characters, `[A-Za-z0-9][A-Za-z0-9_.:-]*`); invocation_id is a canonical lowercase hyphenated UUID. metadata_blobs has1..64 entries, unique digest keys, size_bytes is an integer1..1048576 (never bool/float). Source commit is exactly40 lowercase hex; every SHA is `sha256:` plus64 lowercase hex. Platform is validated by the shared Platform model, including variant, without reinterpreting missing variant.

Allowed runnable media strings are `application/vnd.oci.image.index.v1+json`, `application/vnd.docker.distribution.manifest.list.v2+json`, `application/vnd.oci.image.manifest.v1+json`, and `application/vnd.docker.distribution.manifest.v2+json`. Metadata additionally permits `application/vnd.oci.image.config.v1+json` and `application/vnd.docker.container.image.v1+json`; neither config type is runnable. Reject descriptor cycles, more than8 index levels, missing references or more than64 referenced metadata nodes. A direct manifest may have runnable_reference equal platform_manifest_digest, because those are the same descriptor role in that case; config identity is still independently verified.

Use the same versioned UTF8/sorted-key/compact/integer-only/no-normalization JSON convention for this distinct record; require canonical bytes and compare their SHA to attestation_digest. A record does not contain its own digest or a signature over itself. Detached authenticated build approval/signature verification is separate. Hash equality, builder_id text and invocation UUID ALONE do not authenticate a build. A trusted operator store/enrolled builder verifier must bind this exact record to the source inputs, build invocation and produced image. No caller or ordinary agent can enroll a builder or assert verification=true. Default missing verifier rejects before effects. Test fixtures may inject a test-only verifier and must not be reported as real provenance acceptance.

Bound each metadata blob to1MiB, total metadata to16MiB and record JSON to64KiB; bound descriptor depth/count and total resolver time. Metadata bytes are read by digest from the protected store or a separately verified bounded archive reader, never extracted to agent-selected paths. Unique references only; validate descriptor-declared size/media type and the SHA of ORIGINAL bytes, not reserialized JSON.

## Required proof and comparison

1. Verify trusted record authenticity plus content digest, image role and all shared manifest source/platform/provenance fields. Source commit/repository/context/recipe/inputs must match exactly. Record image_config_id must equal the role's manifest image_config_id.
2. Load the runnable descriptor by its typed digest/media type. Verify every original index/manifest/config byte hash and descriptor link to the UNIQUE selected platform manifest and config; reject ambiguity, wrong architecture/variant, duplicate keys, unsupported media/layout or absent original bytes. An index/config may have the same SHA syntax but never interchangeable roles.
3. Select only the record's preloaded immutable runnable_reference at the exact agreed platform, with pull never. Do not run a config digest merely because it is in the manifest; containerd may not address it. No mutable tag or pull fallback. An unsupported legacy backend is held/rejected rather than renamed into compliance.
4. Correlate actual created-container image selection with that same manifest/config chain and full effective container profile. Generic inspect.Id, container.Image, Config.Image, labels or helper output alone are not universal config proof. If the chosen backend cannot supply adequate actual-selection evidence, stay held/evidence unverified.
5. The existing helper proves metadata relationships only; it does not authenticate layers, a builder, source provenance, resolved state/secrets or a running container. Keep that limit explicit. Never expose archive/config/environment contents in public evidence or logs.

## Additional source boundaries needing agreement

- Probe execution: network-none HTTP requires a fixed compiled in-container probe. Docker exec creates a process and is allowed only in explicit smoke phases, not GET or inspect-only reconcile. Reconcile may use private durable probe evidence bound to the exact instance/image/profile and read-only daemon observations; absent/stale proof remains held/health unknown. No automatic re-probe to convert unknown into success.
- Known failure versus uncertainty: only a proven terminal outcome may return failed and trigger controller rollback. Timeout, daemon unavailability after submission, absent inspect with possible late completion, foreign identity or missing provenance are held/inspect_required/recovery_required. Fresh command/run IDs cannot bypass a phase reservation.
- State provisioning: the fixed app UID/GID10001 must actually own/access its isolated generation. An unprivileged host process cannot assume it may chown host files to10001, and the frozen profile does not authorize a privileged helper container or chown entrypoint override. Require preprovisioned private state storage or an independently approved isolated-runner provisioner; default unavailable fails before effects. No live permissions changes are authorized here.
- Environment: validate effective configuration as verified image defaults plus the exact manifest overrides/test-secret slots. Do not silently accept extra daemon-injected environment or weaken the shared schema. Preserve image-default entrypoint/command and bounded exact probe profile.
- Source tests can validate SQLite/ownership/filesystem logic and injected test-only engine/verifier interfaces. They must not claim actual daemon, image, provenance, app health or rollback acceptance. No ephemeral Docker acceptance until independent review as requested.

Registry remains the existing priority source task. This record contract is the first handoff required by the new assignment; no duplicate registry/pilot worker or live adapter is started by it. The host source candidate must consume these agreed trust boundaries and shared models rather than inventing a second manifest or treating a record hash as authorization.
