# Candidate Hub build inputs observed 2026-11-12

Status: **candidate references for independent review**, not approved build
inputs, a signed upstream attestation, downloaded model acceptance, or a built
image. These observations do not change the existing release workflow. The tag
and model repository were accessed over HTTPS; trust is in the responding
providers and their metadata, not an independently authenticated publisher
signature or a second independent source.

## Node image

Docker Hub's public metadata for `node:24-bookworm-slim` reported:

- Multi-platform OCI index: `sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54`.
- Linux amd64 image manifest: `sha256:0e2646ab57e77b7a384a0cb59b6815b182c77e1f9c3ba36bd8435bd9c4e6680e`.
- Image config: `sha256:5ba3c1ded6eada2ea16582327a87c672fb3889fca362ee3ac0d78ed742b583c0`.
- Config declares Linux amd64 and `NODE_VERSION=24.22.3`.

The OCI index, amd64 manifest, and 6,715-byte configuration were fetched from
Docker Registry and their actual returned bytes hashed to their stated digests.
No image layers were downloaded or independently verified. The tag may move;
an independently reviewed build input can use the explicit platform manifest
as `docker.io/library/node@sha256:0e2646ab57e77b7a384a0cb59b6815b182c77e1f9c3ba36bd8435bd9c4e6680e`,
subject to platform and provenance review.

Sources: [Docker Hub tag metadata](https://hub.docker.com/v2/repositories/library/node/tags/24-bookworm-slim)
and the [Docker Registry manifest endpoint](https://registry-1.docker.io/v2/library/node/manifests/sha256:0e2646ab57e77b7a384a0cb59b6815b182c77e1f9c3ba36bd8435bd9c4e6680e)
(the registry requires an authorization token).

## FastEmbed model

The repository's locked FastEmbed 0.9.0 selects
`Qdrant/bge-small-en-v1.5-onnx-Q` for `BAAI/bge-small-en-v1.5`.
Hugging Face metadata reported repository revision
`aa8f8b060edb00e03bfdd08813a2949946c8ba55`. Candidate snapshot files are:

| File | Bytes | SHA-256 | Observation |
| --- | ---: | --- | --- |
| `model_optimized.onnx` | 66,465,124 | `51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431` | Provider LFS metadata only; file not downloaded. |
| `config.json` | 706 | `13582bcf2effc85b7bf3d3f5532e686bc1c9ce86bb009d10f0ec33cbe92299dd` | Bytes fetched and hashed. |
| `tokenizer.json` | 711,396 | `d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66` | Bytes fetched and hashed. |
| `tokenizer_config.json` | 1,242 | `0b29c7bfc889e53b36d9dd3e686dd4300f6525110eaa98c76a5dafceb2029f53` | Bytes fetched and hashed. |
| `special_tokens_map.json` | 695 | `5d5b662e421ea9fac075174bb0688ee0d9431699900b90662acd44b2a350503a` | Bytes fetched and hashed. |

The four small files were fetched at the exact revision with a one-MiB-per-file
limit, hashed locally, and their Git blob SHA-1 values matched the provider's
metadata. That match is a consistency check with the same provider, not an
independent attestation. The ONNX SHA-256 is solely a provider metadata claim:
its bytes, actual load, and offline inference were not tested. The listed files
match the model and available configuration/tokenizer patterns selected by
FastEmbed; actual compatibility remains to be demonstrated with the reviewed
full snapshot and installed runtime.

Sources: [model metadata](https://huggingface.co/api/models/Qdrant/bge-small-en-v1.5-onnx-Q?blobs=true)
and [fixed revision tree](https://huggingface.co/Qdrant/bge-small-en-v1.5-onnx-Q/tree/aa8f8b060edb00e03bfdd08813a2949946c8ba55).

## Remaining work

Independently review source and ownership, construct and pin the exact model
manifest expected by `scripts/verify_hub_build_inputs.py`, obtain and verify the
full ONNX bytes against the reviewed pin, and independently review the Node
digest and platform. Use a task-owned, bounded builder to build the exact final
source and verify actual offline model inference, installed runtime identity,
UI, executor compatibility, and image digests. The opt-in pinned recipe is not
the main publication workflow; no build, deployment, or cutover occurred in
this metadata review.
