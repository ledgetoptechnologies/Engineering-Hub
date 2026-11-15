# Disposable executor compatibility evidence

Manually dispatch **Verify disposable executor compatibility** from `main` after
review. It does not run on development pushes or pull requests. All source,
wheel, core manifest/config and commit inputs must be explicit immutable values;
source must be an ancestor of the checked-out main commit. The existing job
`GITHUB_TOKEN` needs only `contents: read` and `packages: read`.

The executor recipe copies the selected core's virtual environment onto its
separately pinned Python runtime. Its current Python base digest matches the
reviewed staging control build's base; an independently selected core may use
a different runtime. Check the exact base and installed runtime compatibility
for each core before approval. A matching Python minor version or a successful
static recipe check alone does not establish compatibility.

The default tuple uses reviewed source `40d3d2f`, core manifest `d78d1576`, and
config `c3ebafe5`. Source is read from Git archives/blobs (canonical LF), with
setuptools 84.0.0 downloaded as a binary wheel using its reviewed SHA256 and
`--require-hashes --no-deps`. The wheel build uses no index/dependency resolution
and fixed `SOURCE_DATE_EPOCH=1792972800`. Python is pinned to 3.12.15. The prior
local toolchain used pip 25.0.1; CI records its actual pip version. Any wheel
byte difference fails the expected hash check; it does not silently accept or
replace the reviewed hash. The default wheel is 126151 bytes, SHA256
`e9db6a97b34e723acbf681308dfcc167dc37e2c798063799ab25d150f3940d29`.
Prepared source files, including generated provenance, have mode `0644`.
Prepare and build inside Linux; copying a Windows-prepared tree into a Linux
builder can change POSIX file modes and therefore ZIP bytes.

Only the fixed Hub control package and pinned Python runtime are pulled. The
core config digest, OCI revision, installed build identity and runtime user are
checked before building the existing executor Dockerfile with its trusted
three-file context and build networking disabled. Existing installed-image smoke
checks run nonroot, read-only, with no network, capabilities, host mounts or
operator config, under bounded CPU/memory/PIDs. They import installed code and
check the actual private Unix client API without creating resources or launchers.

The only uploaded artifact is `executor-compatibility-receipt.json`, containing
hashes, tool versions and sanitized outcomes. No wheel, image, credentials or
private files are uploaded. Job-local images disappear with the disposable
runner; nothing is pushed, promoted, exported, enrolled or deployed. Successful
compatibility does not establish registry availability of an executor image,
staging acceptance, private connectivity or a real staging workflow. A pull
failure remains a failure, with no mutable-tag fallback.

Failure receipts retain a fixed substage and failure class, plus a bounded CLI
exit code when available. They never include exception text, argv, stderr, URLs
or private paths. Python/pip versions, canonical archive hash, observed backend
hash and observed wheel hash/size are recorded as each becomes available, before
checking the independently expected wheel hash. A mismatched wheel is still
rejected and never copied into the build context. Missing observations mean the
job did not reach that observation; they do not identify the underlying cause.

The first run (40371204689, workflow source 653832f) failed in the wheel phase
before registry login, pulls or image builds. Its original receipt only said
`wheel_failed`; it cannot establish a hash mismatch or any more specific cause.
That historical failure is preserved rather than relabelled by later diagnostics.

Run 40372414295 identified a wheel hash mismatch. Its canonical archive and
hashed backend matched; the resulting 126151-byte wheel had SHA256 `e9db6a97...`.
Independent byte comparison reproduced that exact hash by changing only the
ZIP mode of `_executor_source.json` from `0777` to `0644` in the original local
wheel (`3a7a04ca...`). Every uncompressed member remained identical; one ZIP
attribute byte changed. A fresh offline Linux rebuild with normalized source
modes reproduced the CI hash. The corrected default pin names those reviewed
bytes, rather than accepting arbitrary build output or bypassing verification.
The original artifact and both failed CI receipts remain historical evidence.
