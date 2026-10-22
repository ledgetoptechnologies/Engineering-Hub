# Container image update observations

Engineering Hub has an advisory image-version read surface separate from health,
staging admission and deployments. A different intended published image is not a
failed container and never authorizes an update.

## Read surfaces

- Authenticated `GET /api/hub/v1/image-versions`.
- The optional `image_versions` field in the authenticated REST overview and
  existing `hub_overview` MCP tool. No additional tool or credential is required.
- States: `up_to_date`, `update_available`, `unknown`. Display as “Up to date”,
  “Update available”, and “Not verified”, with neutral styling, observation age
  and agent-report attribution. Keep application health unchanged.

The read path reevaluates expiry on each request. An open browser must also stop
displaying a positive comparison after `valid_until`; an old report must not be
made fresh by refreshing the page. A lightweight native status refresh is not an
AI inbox polling schedule.

## Existing authenticated report channel

An approved native inventory collector publishes a bounded knowledge document
using the existing knowledge CAS interface:

```
project: engineering-hub
path: observations/image-versions-v1.json
```

The JSON object has `schema_version: 1` and 1–32 `items`. Each item has:

- `target`: `project`, `host`, `service`, `environment` (production or staging),
  GHCR `repository`, and intended published `reference` (tag or manifest digest).
- `running`: an observation with `source: runtime`, aware `observed_at` and
  `valid_until`, and `identity` metadata.
- `published`: an observation with `source: registry`, the matching intended
  `reference`, aware timestamps, and `identity` metadata.
- Either observation may be absent/null, yielding “Not verified”.

Identity metadata includes `repository`, `platform` (`os`, `architecture`, optional
`variant`) and whichever verified `index_digest`, `manifest_digest` and
`config_digest` are available. Digests are full `sha256:` values. Intervals must
be positive and at most 30 minutes. The collector must bind observations to the
actual target host/service; the reader cannot independently attest that fact.

The server derives `reported_by` from the authenticated knowledge record, not an
author claimed in its JSON. Reports are advisory observations, not deployment
receipts or permission records. Never put credentials, full Docker inspect
output, container environment variables or access tokens in this document.

## Comparison rules

Compare matching digest kinds for the same repository and platform. Prefer the
runnable configuration identity, then the platform manifest, then a comparable
index for equality only. Different indexes alone remain unverified until a
platform manifest or runnable configuration can be compared: a new multi-platform
index can still contain the same image for this machine. Conflicting parent/child
identities, unverified platforms, wrong references, expired observations and
unavailable sources stay unknown. Tags and source revision labels alone are not
proof of an exact running image. A GitHub branch commit without a published image
is not an available container update. A difference is not semantic-version
ordering: it means the configured intended published target differs.

References: [Docker image digest and multi-platform identities](https://docs.docker.com/dhi/explore/security-concepts/digests/),
[GHCR metadata and package access](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

## Activation and acceptance boundary

The report reader, API and comparison tests do not install a production collector,
grant Docker/NAS access, create registry credentials or schedule AI checks. Until
an approved collector supplies genuine observations, the status is unconfigured
or unverified. Existing Prometheus node build metrics alone are not container
image inventory. An always-on refresh still requires a verified read-only runtime
inventory source and GHCR metadata access; private packages may require existing
approved read credentials. Do not mount a production Docker socket into Hub.

Owner split: Codex maintains comparison/read contracts, Hermes assesses native
inventory collection, and Claude integrates the neutral dashboard display. Mock
observations exercise software contracts, not live production monitoring.
