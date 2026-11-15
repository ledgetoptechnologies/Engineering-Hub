# Hub build input verification candidate

This input checker is not an image builder or provenance attestation. The main
control-image workflow now uses the pinned recipe and materializes the fixed
public model snapshot as described in [pinned release inputs](pinned-release-model-input.md).
The older `Dockerfile.hub-control` remains available for non-release uses.

Before building a revised recipe, an independent reviewer must supply (1) the
full immutable Node OCI reference `docker.io/library/node@sha256:<64 hex>` and
(2) a model manifest plus its externally retained SHA-256. Never set the latter
to a hash calculated only from the same unreviewed download and call that
independent provenance. Obtain and review the model, publisher/source and exact
files separately; no concrete Node or model digest is supplied here.

The snapshot is a **quiescent, independently protected** directory of ordinary
files with no symbolic links. It must contain `model_optimized.onnx`,
`tokenizer.json`, and `config.json` in its root. Manifest JSON is UTF-8 with
exactly `schema`, `model`, and `files` keys; `schema` is
`engineering-hub.model-snapshot.v1`, `model` is `BAAI/bge-small-en-v1.5`, and
`files` maps every slash-separated relative file path to its lowercase SHA-256.
No unlisted files are accepted. This materialized snapshot is an input format;
it is not claimed to be an already usable FastEmbed cache layout.

```sh
python scripts/verify_hub_build_inputs.py --node-image "$REVIEWED_NODE" \
  --manifest "$MODEL_MANIFEST" --manifest-sha256 "$REVIEWED_MANIFEST_SHA256" \
  --model-dir "$PROTECTED_MODEL_SNAPSHOT"
```

The checker verifies exact inputs and rejects mutable image references, changed,
extra or missing model files, traversal, symbolic links and oversized inputs.
It cannot stop a concurrent writer changing files after verification, prove OCI
platform selection, authenticate upstream origin or prove that these files load
correctly. Its manifest digest is meaningful only if independently obtained.

`docker/Dockerfile.hub-control.pinned` accepts
an explicit Node digest and a BuildKit named context `hub_model` containing
`manifest.json` and `model/`. It verifies the copied context, copies the verified
files to `/opt/fastembed-cache/bge-small-en-v1.5/`, verifies again, and loads
FastEmbed with `local_files_only=True`, offline mode and build networking disabled.
The fallback layout is derived from FastEmbed 0.9.0 and is covered with synthetic
files; only an actual build with the independently reviewed real model can prove
the installed embedding loads. The runtime stage matches the existing recipe.

An operator can supply a reviewed, private named context and these exact inputs
to the opt-in recipe, with a task-owned bounded builder; this is a command shape,
not authorization, a pinned value, or an already executed build:

```sh
docker buildx build --file docker/Dockerfile.hub-control.pinned \
  --build-context hub_model="$PROTECTED_CONTEXT" \
  --build-arg HUB_NODE_IMAGE="$REVIEWED_NODE" \
  --build-arg HUB_MODEL_MANIFEST_SHA256="$REVIEWED_MANIFEST_SHA256" \
  --build-arg VCS_REF="$REVIEWED_COMMIT" .
```

Select a suitable local output and ensure the context contains only the reviewed
model and manifest; do not publish or use a shared builder by accident. Record
the builder, base platform manifest/config, wheel and model inputs, then test
installed code and UI against the exact image digest. The main publication
workflow supplies the reviewed pins and retains main-only Actions. Do not claim
reproducibility or deployed acceptance until the actual build and offline model
loading are proven.
