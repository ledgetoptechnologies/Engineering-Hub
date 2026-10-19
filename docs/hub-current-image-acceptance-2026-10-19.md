# Current application image: local build and recreation acceptance

Exact source: `9d3d951033552496b92d7056478cb4ec2849fc7b`, exported with
`git archive` into a fresh local build directory. Source tar SHA-256:
`de3648830430873bd4770453528a8518737a05acef1c36edfc3171f9852c313c`.
This work follows Beau's direct staging-testing authorization. Hermes owns the
separate staging-host bootstrap; Codex made no competing host changes.

## Actual build

The existing `docker/Dockerfile.hub-control` built successfully with the exact
VCS_REF above. This was a fresh application/dependency/dashboard build, not a
source overlay on the validation image. The committed Python lock resolved 65
packages and installed 57 runtime packages; the dashboard's committed package
lock installed 46 packages, and TypeScript/Vite compilation passed. Build network
access fetched dependencies; no project lockfile was changed. No registry image
was pushed and no installed Hub or MCP was replaced.

Resolved build bases (the Dockerfile's tags themselves remain floating):

- Python: `sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1`.
- Node: `sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e`.

Application artifact identities:

- Index: `sha256:fca79022ce5f9f1ca507f3a5ab8945ce7f8d7e838155e370d1fca1e97a41133e`.
- Platform manifest: `sha256:20eb24142fd821386d64af913d8975a058596f98e1a9d627f52d38b1eca7719d`.
- Config: `sha256:9a8676c5da969e3695f5cbf06871460c0e60fa38d04bf00ee3d13fba9d87a98d`.
- Reported local image size: **490,997,311 bytes**; this is not compressed
  transfer size, total daemon-cache use or peak unpack budget.

The image's revision label and installed health build identity matched the
source commit. These matching claims and locally produced hashes are useful
artifact identification, not independent build approval or signed provenance.

## Executed existing acceptance checks

An independent read-only agent review found no blocker to running the two existing
smokes in their bounded local scope. Both ran against the immutable named digest,
not the mutable local build tag, and both exited zero:

```text
python scripts/hub_release_smoke.py --image ghcr.io/ledgetoptechnologies/engineering-hub-control@sha256:fca79022ce5f9f1ca507f3a5ab8945ce7f8d7e838155e370d1fca1e97a41133e --commit 9d3d951033552496b92d7056478cb4ec2849fc7b
python scripts/hub_split_container_smoke.py --image ghcr.io/ledgetoptechnologies/engineering-hub-control@sha256:fca79022ce5f9f1ca507f3a5ab8945ce7f8d7e838155e370d1fca1e97a41133e --commit 9d3d951033552496b92d7056478cb4ec2849fc7b
```

Default startup checked health, baked source identity, built UI assets, disabled
human sign-in/private API denial and three separately authenticated synthetic
agents listing nine tools. A synthetic marker survived process/container
recreation over the same newly owned named volume.

Split startup checked real agent `0.0.0.0:8770` and browser `127.0.0.1:8771`
bindings, configured Docker isolation/resource limits, missing/forged Access
rejection and rejection of an agent token on the browser interface. An actual
task claim and open non-authorizing decision survived recreation and remained
readable by the three synthetic agent identities.

Containers used network none, UID/GID568, read-only root, dropped capabilities,
no-new-privileges, 512 MiB memory/no extra swap, one CPU, 128 PIDs and a bounded
scratch tmpfs. Only new ownership-labeled named volumes were mounted; no host
directory, Docker socket or public port was exposed. Owned fixtures were removed;
post-check container/volume queries found no test residue. The image is retained.

## Not yet established

### Transfer artifact prepared locally

The exact named digest was exported with Docker image save to a new local file,
`C:/Users/fstor/Documents/Codex/eh-control-image-9d3d951-fca790.tar`.
Its actual transfer length is **117,802,496 bytes** and SHA-256 is
`1d9c3e421e234b797a674c650ee4973e58b94c67cbe83ac7dda24058cd35cf8a`.
The existing bounded archive-inspection helper independently resolved the OCI
index/platform-manifest/config chain to the identities above without extracting
files or importing an image. That helper intentionally skips layer bytes.
A separate streaming read additionally hashed all **15** exported SHA-256 blobs
(117,785,165 content bytes), including layer payloads, and every digest matched
its canonical blob name. No duplicate blob name was accepted. This verifies
export integrity, not trusted source provenance or a destination import.
No transfer to staging or remote daemon operation occurred.

The pilot manifest requires distinct candidate/rollback image config IDs; this
single image cannot fill both roles. Independent review request
`61caa9af-a05a-4a71-a5b1-5103dc0c1a5a` asks Claude to assess the concrete next
installation/enrollment requirements. Awaiting that response is not evidence
of an active execution process or permission to duplicate Hermes's host work.

### Deployment and pilot limits

The default image entrypoint still starts the combined listener; split smoke
explicitly selects split startup. Do not install the default command as proof
of the intended private-browser boundary. This does not verify genuine Cloudflare
human sign-in, a tunnel/TrueNAS deployment, provider/issuer enrollment, autonomous
rollout/health/rollback, host power loss, or PC reboot. No production tokens,
credential policy or subscriptions were changed. Full runtime suite evidence
remains separate from these two executed application-image checks.
