# Offline development CI through modular staging

This is a source candidate, not an enrolled recipe or measured resource claim.
It preserves the existing Hub WorkQueue and modular worker, and leaves the older
`scripts/local_ci_container.py` `/input/source.tar` interface unchanged.

`scripts/modular_ci_container.py` is copied into a reviewed cache image at
`/opt/eh-ci/modular-bootstrap.py`. The worker already proves the daemon, actual
kernel limits and mounts before copying `/source` into writable `/workspace`
and invoking configured commands. Source never chooses the image, bootstrap,
host commands or network. Both stages verify all four exact committed dependency
hashes against the image-owned `modular-cache.json` before running any source
configuration, tests or package script. The v2 cache manifest also binds the
bootstrap and both image-owned Python/Vitest reporter hashes. A v1 cache is not
silently upgraded or accepted for the new command contract.
No dependency installer runs in either stage.

The two fixed recipe commands are:

```json
{
  "build_argv": ["/opt/validation-venv/bin/python", "-I", "-B", "/opt/eh-ci/modular-bootstrap.py", "build"],
  "test_argv": ["/opt/validation-venv/bin/python", "-I", "-B", "/opt/eh-ci/modular-bootstrap.py", "test"]
}
```

The build stage runs `npm run build -- --base ./` (the repository's TypeScript
check and Vite production build) and exports the static output plus a binding
summary to `/output`. Relative asset URLs support the existing preview gateway.
The test stage verifies that summary, then runs the **entire Python pytest suite**
and `npm test` sequentially, even if Python tests fail. Vitest uses one worker and
disables file parallelism. No tests are selectively substituted for the suite.
Existing environment-based skips are reported as skips, not runtime acceptance.
Playwright browser installation/e2e and image-release smoke checks are outside
this recipe; it does not claim parity with every hosted release-workflow step.

Bounded JSON reports, stage summaries and responsiveness artifacts go to
`/test-output`; unbounded JUnit output is no longer requested. The image-owned
Python launcher loads installed pytest before source import paths, then passes
the unchanged full-suite arguments to `pytest.main`. It records outcome counters,
a summary capped at 64 KiB and failure details capped at 128 KiB total/4 KiB per
entry. The Vitest reporter has a 512 KiB total detail cap and 64 KiB summary cap.
Omitted/clipped details and incomplete outcome accounting are explicit. Child
exit zero without a valid complete passing report fails the CI stage, and UI
tests still run after a nonzero Python result. Atomic summary publication avoids
empty final files on a write failure. This does not reserve physical capacity.

Console output remains subject to the worker's log bound. A nonzero test exit
fails the operation. Worker resource manifests bind exported files; these JSON
reports do not add server-verified commit provenance. Source remains `declared`.
Timeout/OOM/export overflow is a failed or held job, never a passing check.

Image dependency packages remain read-only. A job-local `node_modules` facade
links their top-level entries and leaves Vite/Vitest temporary caches writable
inside `/workspace`, avoiding a full node_modules copy into the workspace tmpfs.
An existing source `node_modules` tree is refused. This needs real Node/Vite
acceptance with the exact prepared image; synthetic tests do not prove runtime
resolution or performance.

## Matching image preparation and attestation

1. Package the committed ref using `scripts/package_staging_source.py`. Its
   `dependency_sha256` and `required_cache_labels` are the expected exact inputs.
2. Independently prepare a dependency cache with the matching locked Python test
   environment at `/opt/validation-venv`, Node/npm and UI packages at
   `/opt/eh-ci/node_modules`, and the four-path digest map at
   `/opt/eh-ci/dependencies.json`. The currently retained older cache mismatches
   current Python inputs and is not a valid base. Dependency acquisition, if
   needed, belongs to separately controlled image preparation, not a job.
   The complete suite also requires a working Git executable. The cached Linux
   diagnostic image was observed without it: packaging checks raised
   `FileNotFoundError: git`. Candidate preparation now checks `git --version`
   offline and refuses that cache; it does not install the missing tool. A newly
   reviewed cache with Git remains necessary before full-suite acceptance.
3. The candidate `docker/Dockerfile.modular-development-ci` consumes that exact
   reviewed immutable base, the four committed lock inputs and this bootstrap.
   Its `--network=none` preparation step refuses mismatching inputs and emits
   `modular-cache.json`. It installs nothing. Build context should contain only
   these named inputs, including both reporter files. Supply the full immutable base reference,
   `PREPARED_SOURCE_COMMIT` and independently computed `BOOTSTRAP_SHA256`.
4. Retain an attestation binding the actual final image ID/digest, base image,
   Python/Node/npm versions, dependency map/cache labels, preparation commit,
   bootstrap and cache-manifest hashes, and exact no-network acceptance result.
   Compare actual image labels/manifest with the package metadata before
   enrollment; no mutable tag or caller-provided manifest establishes a cache.
5. Enroll only that image ID under a fixed owner-configured recipe on the
   existing physical machine/queue. Existing project execution grants remain
   necessary. No second provider, old held-runtime alias or privileged daemon
   is introduced.

## Resource acceptance remains necessary

The previous measured candidate used one CPU, 512 MiB memory/no swap, 64 PIDs,
128 MiB workspace, 16 MiB output, 512 KiB logs, 64 MiB `/tmp` and a 900-second
per-stage deadline. The real full-suite run exhausted temporary and report space;
this envelope is insufficient. Bounded reporting addresses report growth, not
the separate need for quota-controlled disk-backed scratch, durable reservations
or a physically reserved worker terminal receipt. No scratch mount or host quota
is introduced by the reporter change. `/test-output` retains output/4. The worker,
daemon, job and exporter stay within the existing 1 GiB aggregate; do not raise
the parent limit to accommodate this profile. Exported output volumes and
workspace tmpfs also consume bounded memory. A full-suite failure under `/tmp`
noexec, tmpfs capacity or the memory ceiling must be diagnosed honestly; do not
silently skip tests, disable kernel checks or reuse the old 2 GiB runner.

An eventual scratch implementation must bind an operator-provisioned dedicated
filesystem slot, verify its actual capacity/mount identity and retain its lease
across held/interrupted work. A plain host directory or Docker volume is not a
disk quota. Failed diagnostic registration/download and ENOSPC-safe worker receipt
publication remain separate work; a source-visible JSON file supplies neither.

Before image import, size the exact prepared artifact and retained layers against
the TEST volume and underlying host free floor. No build, pull, image import,
recipe enrollment or full-suite runtime acceptance has been performed by adding
these source files.
