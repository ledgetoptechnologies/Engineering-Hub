# Hub build input verification candidate

This is an opt-in input checker, not an image builder, provenance attestation,
or change to the existing release workflow. It does not modify the current
Dockerfile or enforce its use. The present control recipe still uses the mutable
`node:24-bookworm-slim` reference and downloads a FastEmbed model while building.

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

An eventual integration must add a separately reviewed immutable Node argument
and copy the verified model into a task-owned build context, bind those exact
bytes to a specific recipe and source commit, and use an offline FastEmbed load
inside the actual image (`local_files_only=True`, with network disabled). Verify
the model against the external pins again inside that build, record the builder,
base platform manifest/config, wheel and model inputs, then test installed code
and UI against the exact image digest. Any workflow change must retain main-only
Actions and avoid claiming reproducibility until those steps are proven.
