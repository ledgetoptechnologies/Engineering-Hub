# Optional executor wheel preparation

This separate packaging project installs the Linux-only `hub_container_executor`
library and its existing `scripts.inspect_hub_image_identity` dependency. It does
not change the core distribution, provide a service entrypoint, provision an
issuer, or activate an executor. `agent-message-bus==0.6.0` must be installed from
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
resource factory, and identity verifier from installed paths. Imports should be
inert. Actual resource construction requires Linux, UID/GID 10001, explicit
private stores, independently enrolled builds, daemon binding and a real identity
provider. Packaging tests and import checks do not prove real Docker deployment,
launch identity, rootless mapping, or reboot recovery.
