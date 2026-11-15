# Local FastEmbed snapshot evidence, 2026-11-12

This is a task-owned **local input and Windows inference check**, not a built
Hub image, Linux image acceptance, deployment, or independently authenticated
publisher attestation. It supplements
[`hub-build-input-candidates-20261112.md`](hub-build-input-candidates-20261112.md).

## Acquired input

Five files were acquired over HTTPS from the fixed Hugging Face repository
`Qdrant/bge-small-en-v1.5-onnx-Q`, revision
`aa8f8b060edb00e03bfdd08813a2949946c8ba55`. Each downloaded file's actual
length and SHA-256 matched the reviewed candidate pins in the accompanying
provenance note. In particular, `model_optimized.onnx` was **66,465,124 bytes**
with SHA-256
`51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431`.
The five files total **67,179,163 bytes**.

A generated `engineering-hub.model-snapshot.v1` JSON manifest enumerates those
five file hashes. Its actual SHA-256 is
`f40477aa8dd45b6000f16892cf3aa6ecfa310e3965baa5fded84fbc2147a203f`.
The opt-in input verifier checked the exact manifest and all five files before
and after inference, using the candidate Node reference
`docker.io/library/node@sha256:0e2646ab57e77b7a384a0cb59b6815b182c77e1f9c3ba36bd8435bd9c4e6680e`.
That Node reference is syntax-checked by the verifier; this run did not download
or run the Node image.

The task-owned input currently resides **only on this Windows host**, outside
Git, at
`C:/Users/fstor/.codex/worktrees/hub-candidate-input-provenance/model-acquisition-20261112/`.
`manifest.json` is at that directory's root and the snapshot is under
`cache/bge-small-en-v1.5/`. Retain it for the pending reviewed build; do not
assume another machine has it or that it has remained unchanged. Reverify the
manifest and exact files before every use. No model bytes are committed here.

## Actual local inference

The existing local Windows environment reported Python **3.13.14**, FastEmbed
**0.9.0**, and ONNX Runtime **1.31.0**. FastEmbed resolved the task-owned
`cache/bge-small-en-v1.5/` directory using `local_files_only=True`,
`HF_HUB_OFFLINE=1`, the CPU execution provider, and one thread. Two synthetic
inputs each produced a 384-dimensional embedding with all values finite.
Python socket `connect` and `connect_ex` were disabled during the test, and
the cache paths were confined to the task-owned directory; this is **not an
OS-enforced network sandbox**. No shared model cache was used.

The downloaded file digests were compared to candidate pins obtained from the
same upstream provider's HTTPS metadata; this proves byte consistency with
those reviewed pins, not independent upstream authorship or signature validity.
This check does not prove the opt-in Docker recipe builds, the pinned Linux
Python 3.12 runtime can load the model, the final image contains those bytes,
or the actual Hub can execute a staging or production workflow. Those remain
separate acceptance steps.
