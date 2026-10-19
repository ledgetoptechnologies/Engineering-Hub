# Optional executor wheel: local packaging acceptance

Source: `5fc6baac19e60531a3e80a8d452aeb16dd76be2a` on the development
branch only. No core package discovery, production Dockerfile, launcher,
entrypoint, host, credentials or deployment changed.

The separate project under `packaging/hub-executor` now builds the optional
`engineering-hub-container-executor==0.1.0` distribution. Preparation uses an
exact Git archive and stages only executor Python source, its single existing
`scripts.inspect_hub_image_identity` dependency, packaging metadata and input
hashes. The archive comment and embedded hashes are not independent provenance
or approval. Output parents must be trusted/exclusively controlled; publication
does not supply atomic no-replace protection against concurrent writers.

Independent review found a path-alias overwrite issue in the first implementation:
normalized paths containing `./` or repeated separators retained different map
keys but wrote the same destination. Source now rejects noncanonical names before
selection; both regression cases pass. Existing-output preservation, links,
duplicates, missing inputs, revision mismatch and fixed packaging scope are tested.

## Executed evidence

- Focused packaging/bootstrap checks: **28 passed**, 0.16 seconds, using the
  existing Windows environment and a fresh explicit test directory. An earlier
  default-temp run had 12 setup errors because the existing Windows pytest temp
  parent was inaccessible; no product assertion failed and no permissions changed.
- Actual Linux wheel build used uv **0.11.17**, cached setuptools **84.0.0**,
  network-disabled build steps and immutable local validation base
  `sha256:2018849b91d01d02c8385e1662a8a57b25f25949069bea3796f1e98776c3462f`.
  An initial bare-digest FROM was incorrectly interpreted as a repository and
  failed before packaging; the corrected named digest resolved the local base.
- Built wheel `engineering_hub_container_executor-0.1.0-py3-none-any.whl`:
  SHA-256 `8af6a2f292129584e4b35c2fbf1d1ec1d08d42425f6bf6ee3f98297c975ed2ea`.
  Installed into the base's venv with `uv pip install --offline --no-deps`.
  The same wheel was exported locally; its independently read SHA-256 matched.
  The owned never-started export container was removed by its verified ID;
  all test containers are terminal/absent. The wheel and local image are retained.
- Local verification image index:
  `sha256:96c56b0f40d778756240c1850b4d5bf680a45ab1033a88b129fc1ea22ab83493`;
  platform manifest `sha256:430c1a8b884f53b5df61529355a96cd12d29a49b8580a913ab186a6f57b76bba`;
  config `sha256:bd24078be21e4fb40588e1363208fd9c065c4c5cff8d90a2c147a8cce1a79590`.
  This is a local validation-derived image, not a minimal production executor
  image or registry publication.
- A UID/GID 10001, network-none/read-only/capability-dropped container ran Python
  `-I` from `/tmp` with no checkout on its import path, no mounts or published
  ports, one CPU/512 MiB/no extra swap/64 PID limits and a 32 MiB scratch tmpfs.
  Service, local resource factory, launch-receipt verifier and inspection helper
  all imported from installed venv site-packages. The probe rejected bind,
  connect, process-launch and shell-execution audit events; none occurred.
- Embedded source hashes matched installed source bytes. All **24 hashed RECORD
  entries** verified against wheel contents; no console entrypoints or package
  `__main__` were present. The first probe's path assertion accidentally matched
  the venv name; correcting its path-component check passed without product edits.

## Remaining gates

The same-version core dependency is not sufficient evidence of the correct core
release. Independently pin its artifact and the optional wheel/image before
enrollment. The inspected core in this local image is the previously verified
validation base, not a newly published core wheel. The namespace `scripts` helper
must not be shadowed by another installed package; actual origin was checked here.

This wheel is an inert Linux library despite its pure-Python wheel tag. Import
success is not independent issuer authentication, a launch receipt, correct
rootless UID mapping, protected host paths, effective machine reservations,
real deployment/health/rollback, installed UI or reboot acceptance. There is no
default service launcher or provider stub. Host bootstrap remains unapproved;
see [the proposal](staging-bootstrap-proposal-2026-10-19.md). No full runtime suite
was rerun for these packaging-only changes; earlier runtime evidence is separate.
