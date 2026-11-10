# Package committed source for modular staging

Run locally, with Git and Python already installed. Use a reviewed main-release
copy of this script in an operator directory, separate from the candidate
checkout. Do not execute a packager supplied by the unreviewed branch itself.
Python isolated mode excludes ambient Python imports; Git remains an explicitly
trusted local executable.

```console
python -I -B /path/to/reviewed/package_staging_source.py --repo /path/to/candidate --ref feature/example --output /path/to/new-staging-package
```

The output directory must not exist. The command resolves the ref once to a
40-character commit and writes `source.zip` and `metadata.json`, including a
base64 commit-object proof. Pass that proof as `source_commit_object_base64`
along with an independently reviewed task ID and exact revision. The proof
establishes object/tree consistency only, not repository membership or a
signature. It never contacts
staging, submits a job, installs dependencies or reads working-tree file content.
Keep packages private when the committed source is private. A disk/write failure
can leave an incomplete new output directory; it is not a successful package and
must not be submitted. Existing output directories are never overwritten.

The ZIP contains exact committed blob bytes, including executable modes, without
newline conversion. Git object plumbing avoids hooks, filters, `export-subst`
substitution and `export-ignore` omissions: both export attributes are deliberately
not applied, including attributes in `.git/info/attributes`. This differs from
`git archive`. Empty directories are not represented by Git. Submodules, symbolic
links, LFS pointers, non-UTF-8/unsafe paths and case-insensitive path collisions
are rejected. Nothing fetches missing LFS or submodule content. Missing partial-
clone objects fail closed: lazy fetch is disabled, all Git transports have an
empty explicit allowlist, and each read-only Git subprocess has a 30-second
deadline covering output and exit, with bounded output capture.

Limits match modular source admission: 8 MiB ZIP, 64 MiB expanded regular files,
and 5,000 entries. ZIP metadata is deterministic. Blob object hashes are checked.
The Git repository and Git executable are local trusted inputs; this is not a
signature check, a clean-history assertion or provider-side provenance proof.

Metadata supplies `source_commit` and `source_digest` for a later separately
authorized staging request. Base64-encode the exact ZIP for
`source_zip_base64`; the operator still selects the registered project/recipe,
request UUID and optional topic. There is no new queue or execution permission.
Server commit verification remains false and worker commit verification remains
`declared`; a local Git check cannot authenticate the uploaded commit to the Hub.

The four committed dependency inputs (`pyproject.toml`, `uv.lock`,
`hub-ui/package.json`, `hub-ui/package-lock.json`) are hashed independently.
`required_cache_labels` lists the exact existing development-CI label values a
reviewed dependency cache must match; `dependency_binding_sha256` hashes canonical
sorted compact JSON of the path-to-digest mapping. This command does not inspect
or approve an image, so `cache_image_verified` is false. Current lock changes
require a matching reviewed cache; do not weaken digest checks to reuse an older
cache. A ZIP alone does not make the static staging recipe run repository CI.
