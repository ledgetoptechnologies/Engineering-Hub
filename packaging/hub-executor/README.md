# Optional executor wheel preparation

This separate packaging project installs the Linux-only `hub_container_executor`
library and its existing `scripts.inspect_hub_image_identity` dependency. The
package requires the explicit `hub_container_executor.installed_runner`,
`installed_coordinator`, `launch_supervisor`, and `launcher_cli` modules plus the direct
launch-receipt transport. A self-consistent wheel or archive missing these
installation components is rejected. It does not change the core distribution,
provide a console-script wrapper, provision an issuer, or activate an executor.
The explicit `python -I -B -m hub_container_executor.launcher_cli` operation is
documented in [the host-launcher contract](../../docs/executor-installed-launcher.md)
in the source repository. Packaging it does not install a root service, generate
enrollment or keys, or authorize activation. Its bounded wait is not reboot or
busy-work drain recovery.
`agent-message-bus==0.6.0` must be installed from
the independently pinned core artifact, not merely resolved by version from a
registry. The version requirement alone does not authenticate that artifact.

From a repository containing the committed packaging files, prepare an exact
40-character commit (not a mutable checkout):

```text
python scripts/build_hub_executor_wheel.py --revision <commit> --output-directory <new-directory>
```

The command only prepares a fresh build context using `git archive`; it does not
build, install dependencies, access the network, or launch services. It stages
tracked executor Python files, the single image-inspection helper, this packaging
metadata, and source provenance. It does not stage other scripts or fixtures.
Use a trusted, exclusively controlled output parent. The freshness checks and
rename do not implement atomic no-replace publication against concurrent writers.

In an independently approved local Linux build environment with uv 0.11.17 and
setuptools 84.0.0 already cached, build using `uv build --offline --wheel
<prepared-directory>`. Do not download build tools implicitly. Pin the resulting
wheel SHA-256 and independently pin the core wheel and OCI image by digest.
Embedded commit/archive hashes describe the input; they are not issuer evidence.

Verification must install the wheel outside the checkout and import the service,
resource factory, identity verifier, installed runner, and launch transport from
installed paths. Imports should be
inert. Actual resource construction requires Linux, UID/GID 10001, explicit
private stores, independently enrolled builds, daemon binding and a real identity
provider. Packaging tests and import checks do not prove real Docker deployment,
launch identity, rootless mapping, or reboot recovery.

The module is not an approved image or installed host service. Running it requires
explicit protected configuration, independent release enrollment, and the direct
issuer exchange before mutable resources are constructed. The root-only launcher
API may be present in this library, but its host installation and independently
protected enrollment/signing key remain separate; packaging source does not grant
that authority or activate it.

## Optional consumer image recipe

`docker/Dockerfile.hub-executor` is a separate, opt-in image recipe. It does not
change an existing Hub image, install the host issuer, enroll a release, or
activate a service. Prepare a trusted new build context containing just that
recipe as `Dockerfile`, the independently approved wheel as `executor.whl`, and
the reviewed `scripts/verify_hub_executor_wheel.py` as
`validate_executor_wheel.py`. Do not build against a mutable checkout or supply
an unreviewed validator. The fixed wheel version is currently 0.1.0.

Supply mandatory build arguments `CORE_IMAGE` (an independently approved full
immutable digest reference), `EXECUTOR_WHEEL_SHA256` (64 lowercase hex
characters) and `SOURCE_COMMIT` (the independently approved 40-character commit).
The validator checks wheel bytes, contents and recorded source identity before
an offline, dependency-free installation. Embedded metadata is not independent
provenance; approval of the expected wheel hash and commit remains external.

There is no `CORE_IMAGE` default. The former `f40ada02...` core (main
`cb7b790...`) predates current-observation APIs and is incompatible with this
executor, despite the unchanged 0.6.0 package version. Preparing a new optional
wheel does not build or select a new core image. Supply a separately built,
approved compatible core by full digest; do not substitute a mutable tag or
guess an unpublished digest. This stage is only a source of `/opt/venv`.
After overlaying the optional wheel, an offline inert import check runs against
the actual installed venv, requiring core observation/transport/reconciliation
modules and every Python module recorded in the verified optional wheel. A
missing API fails the image build; no operator configuration or issuer/runner
resource factory is invoked. This is compatibility, not enrolled build identity.
Registry access may
require authentication configured outside the context. A locally validated
alternate core digest is a different input, not equivalent provenance or release
enrollment. Record the exact core and Python runtime/platform identities used.

The recipe copies the pinned Python 3.12 runtime filesystem into a scratch final
stage, copies the core venv and overlays the verified executor wheel. This resets
inherited image configuration rather than relaxing the launcher's environment
allowlist. Final defaults are only the explicit PATH, working directory `/`,
UID/GID 10001, and direct exec-form installed-runner command with
`--config /operator/runner.json`. There is no console wrapper, checkout fallback,
model download, exposed port, healthcheck, declared volume or default config.
The library's root-only issuer API may be present, but no issuer is provisioned,
configured, started or granted a key in this consumer image.

Building the recipe is not acceptance. Independently authenticate the resulting
OCI chain and installed runner hash; test installed imports and exact image
defaults without checkout access. Real PID1 receipt exchange, rootless mappings,
effective mount/cgroup permissions, provisioned TEST slots, resource budgets and
real validate/deploy/health/rollback remain separate gates. The recipe does not
grant access to a Docker daemon or any host path by itself.

## Offline installed-image compatibility check

After loading the explicitly built executor image, run:

```text
python scripts/hub_executor_image_smoke.py --image engineering-hub-executor:build-<unique-build> --commit <exact-40-character-source-commit>
```

The helper never pulls. It resolves the selected build tag (or full executor
digest reference) to its local image ID, checks fixed runner defaults, and runs
one fixed inert import probe under UID/GID 10001, isolated Python, read-only root,
no capabilities, no new privileges, no network, no host mounts or secrets, and
256 MiB/no additional swap, one CPU and 64 PIDs. It checks installed module paths
and source hashes against the packaged source record and refuses a core lacking
`ContainerPilotClient.for_private_unix`. It verifies the actual container's
effective isolation/resources and removes only its independently labelled test
container. It does not invoke runner/coordinator serving or the root supervisor.

This is artifact consistency/compatibility, not independent build approval,
issuer exchange, kernel mapping/cgroup acceptance for the intended installed
topology, a real queued deployment, TrueNAS integration or reboot recovery.
The helper is not a general command loader, executor installer or a replacement
for the independently approved immutable release enrollment.
