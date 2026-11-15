# Local bounded builder and OCI core preflight

This is preparation, not a running builder, image build, release, or staging
acceptance. Recheck computer load and disk and confirm task ownership before
starting any process. Use a separate unique builder; do not select, stop,
reconfigure, or prune another session's builder, cache or Docker services.

`docker/hub-local-buildkit.toml` limits worker parallelism to one. The Docker
container driver supports CPU and memory limits. This illustrative creation
uses a reviewed immutable BuildKit image reference and does not change the
default builder (`--use` is intentionally absent):

```sh
docker buildx create --name "$TASK_BUILDER" --driver docker-container \
  --buildkitd-config docker/hub-local-buildkit.toml \
  --driver-opt "image=$REVIEWED_BUILDKIT_IMAGE" \
  --driver-opt memory=2g --driver-opt memory-swap=2g \
  --driver-opt cpu-quota=50000 --driver-opt cpu-period=100000
```

This runs on the shared Docker Desktop engine and is **not** a host-security or
staging worker-containment guarantee. Verify the created container's identity,
effective cgroup and resource settings after bootstrap; record its IDs and its
task-owned cache volume. Do not assume those settings propagate to every build
worker without verification. Dependencies and base images may need network;
the model verification/load stage alone declares `--network=none`. Docker
documents [driver resource options](https://docs.docker.com/build/builders/drivers/docker-container/).

Freeze and archive the exact reviewed source, use the externally reviewed Node,
model manifest and materialized snapshot as described in
[`hub-offline-build-inputs.md`](hub-offline-build-inputs.md), and export the
control image as a task-owned OCI layout. Record the raw OCI manifest digest,
config digest, source commit and build evidence. Do not assume a local image tag
is equivalent to an OCI manifest digest.

## Executor core identity check

Before giving a local OCI layout to the executor build, independently select
its exact platform image-manifest digest and a syntax-valid immutable core
reference ending in that same digest. On a trusted, private, quiescent local
layout, run:

```sh
python scripts/verify_local_core_oci.py \
  --layout "$CORE_OCI_LAYOUT" \
  --core-image "$IMMUTABLE_CORE_REFERENCE" \
  --context-digest "$REVIEWED_CORE_MANIFEST_DIGEST" \
  --source-commit "$REVIEWED_CORE_SOURCE_COMMIT"
```

The checker fails unless the reference digest and context digest are identical,
the manifest occurs exactly once in the OCI index graph, and the hashed OCI
manifest, config and every layer match their descriptor sizes and digests. It
also requires Linux amd64, user `568:568`, and the expected OCI revision label.
The verification is read-only and bounded, but the layout must be protected
against concurrent writers through the eventual build; the checker is not a
race-free fence or provenance attestation. A revision label does not prove the
installed package identity or establish where the image was built.

Buildx can consume a named context from an OCI layout by digest:

```sh
--build-context "core=oci-layout://${CORE_OCI_LAYOUT}@${REVIEWED_CORE_MANIFEST_DIGEST}"
```

Docker documents that named contexts can override stages, including an OCI
layout source: [Buildx build reference](https://docs.docker.com/reference/cli/docker/buildx/build/).
The executor Dockerfile's stage is named `core`; its wheel validator checks the
`CORE_IMAGE` string but does not itself prove that a caller's named context has
the same digest. The invoking workflow must pass exactly the checked arguments,
retain the layout unchanged, and verify the produced image's actual installed
core identity and imports. A synthetic or live test of the complete override is
still required. Never substitute a mutable tag or call this installed acceptance.

When finished or interrupted, stop only the uniquely created task-owned builder
and verify its children exited. Retain required OCI output and evidence. Clean
only verified task-owned temporary resources; if ownership is unclear or a
cleanup request is rejected, leave them intact and report the limitation.
