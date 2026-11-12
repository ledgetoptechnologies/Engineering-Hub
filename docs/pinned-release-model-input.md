# Pinned model input for main image builds

Both main-only control-image verification and publication materialize the same
five files from the fixed Hugging Face revision encoded in
`scripts/materialize_hub_model.py`. The checked-in manifest and immutable Node
reference pin the bytes used by `Dockerfile.hub-control.pinned`. The build fails
if the public source is unavailable or any file differs. Each GitHub-hosted job
downloads approximately 67 MB into its own temporary directory; no GitHub token
is sent to the model source. The Dockerfile repeats verification and performs
model loading without network access.

The file digests were originally checked against metadata from the same upstream
provider. They establish byte consistency, not independently authenticated
publisher provenance. A successful build does not imply staging acceptance.
The publication job separately builds and tests its exact candidate and verifies
the pushed immutable digest; its digest is not assumed to equal a local build.
