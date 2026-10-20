# Optional executor wheel preparation

This separate packaging project installs the Linux-only `hub_container_executor`
library and its existing `scripts.inspect_hub_image_identity` dependency. The
package includes the explicit `hub_container_executor.installed_runner` module
and direct launch-receipt transport. It does not change the core distribution,
provide a console-script wrapper, provision an issuer, or activate an executor.
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

Supply mandatory build arguments `EXECUTOR_WHEEL_SHA256` (64 lowercase hex
characters) and `SOURCE_COMMIT` (the independently approved 40-character commit).
The validator checks wheel bytes, contents and recorded source identity before
an offline, dependency-free installation. Embedded metadata is not independent
provenance; approval of the expected wheel hash and commit remains external.

`CORE_IMAGE` defaults to the published Hub control artifact at
`ghcr.io/ledgetoptechnologies/engineering-hub-control@sha256:805d1f825d9b4ac29061de36fd637873c746a8f44dcbd61b7d54053d24f1b45c`.
An explicit override must also be a full digest reference. This stage is only a
source of `/opt/venv`, never executed during the build. Registry access may
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
