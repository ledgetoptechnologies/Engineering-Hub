# Connect project components and field reviews

The Hub remains the human login, project catalog and durable answer ledger.
The staging PC serves registered immutable assets over private TLS. Phone users
visit the existing Hub hostname; they need neither LAN access nor a second login.
The Hub never forwards its human cookies or agent tokens to staging.

Keep the existing Hub Compose, ports 8770/8771 and Engineering-Hub volume.
The optional file below lives beside `hub.sqlite3` in that existing volume.
Do not add the retired Agent_MCP mapping or an alias listener. No new Cloudflare
hostname, tunnel or Access policy is required for these native review pages.

## Enroll once, after the published Hub update

1. Create the desired umbrella projects/components in Projects. Existing
   Viewer/TreeDAR project histories can be linked through `legacy_project_id`;
   they are not moved or deleted. Components are metadata, not execution grants.
2. Install the separately packaged `hub-staging-assets` native service on the
   staging PC using `docs/staging-assets-native.md`. Its dedicated TLS key and
   credential stay outside repositories/chat. Restrict peers to the NAS and
   explicitly named local acceptance clients. It exposes no execution endpoint.
3. Place the trusted public CA certificate and dedicated asset credential in
   protected files readable by Hub UID568. Write `interactive-service.json`
   with explicit project/actor grants and registered immutable asset metadata.
   The configuration/CA must be owned by root or UID568 with no group/other
   write; the credential must additionally exclude all group/other permissions.
   Do not copy private staging TLS keys onto the NAS.
4. Recreate the existing Hub app. Missing or invalid optional configuration
   disables this integration while ordinary Hub/MCP work remains available.
5. Verify the actual NAS-to-staging TLS path, authorized mobile review, logout,
   unrelated-project denial, answer persistence and component revision fencing.
   Local/source tests do not establish that these installed paths are working.

Configuration shape (replace IDs/paths privately; no tokens in this file):

```json
{
  "schema": 1,
  "enabled": true,
  "origin": "https://192.168.50.90:8790",
  "ca_file": "/var/lib/hub/private/staging-assets-ca.pem",
  "credential_file": "/var/lib/hub/private/staging-assets.token",
  "grants": {
    "your-project": {
      "codex": ["review_read", "review_create"],
      "hermes": ["review_read", "review_create"],
      "claude": ["review_read", "review_create"],
      "human:42": ["review_read", "review_answer"]
    }
  },
  "assets": {
    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee": {
      "project": "your-project",
      "component_id": "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee",
      "size": 123,
      "sha256": "REPLACE_WITH_64_LOWERCASE_HEX_CHARACTERS",
      "media_type": "application/json"
    }
  }
}
```

For forms without staging assets, omit origin/CA/credential and use an empty
`assets` object. The `human:42` above is synthetic: use the actual authenticated
Hub actor ID. Never infer an owner from the first person who visits a page.
Configuration is loaded on startup; grant changes require a Hub restart.
Logout and component status/revision changes are checked on each request and
again while streaming assets. A removed configured asset revokes its use after
restart. Its UUID remains bound to the original metadata fingerprint in the
Hub ledger; replacement evidence requires a new asset UUID and review.

## Agent and human workflow

Agents use `hub_component_list/get` to identify a component and
`hub_interactive_review_create` to submit a typed, version-bound review.
`hub_interactive_review_list/get` retrieve authorized progress and answers.
Humans save choice/text answers through EH with existing session/CSRF and
revision checks. A submission records who answered and when; it is data, not
permission to deploy software. Global activity contains pointers, not answers.

The native pointcloud view accepts a bounded sample:

```json
{"schema":"pointcloud.v1","points":[[0,0,0],[1,2,3,30,180,70]]}
```

Coordinates must be finite; optional RGB values are 0–255. At most100,000 points
and8MiB JSON are rendered. Drag rotates; buttons/keyboard zoom and reset. This
is a review sample renderer, not a full LAS/LAZ or tiled Potree viewer. Large
registered assets support strict single byte ranges of at most8MiB through the
typed asset endpoint; no arbitrary URL or filesystem proxy is available.

## Organization and retention

Use project → component → review instance → immutable asset. Keep durable
questions/answers in EH and large bytes on staging. Components retire without
deleting history. Static development previews remain on the existing opaque
sandbox path; custom scripts do not inherit EH's origin or cookies.

The existing staging resource cleanup workflow protects active/HELD work,
human pins and diagnostics and deletes only verified owned terminal resources.
This read-only asset service adds no delete/prune endpoint. Removing expired
asset bytes requires checking all review references and an explicit owned
cleanup plan; do not blanket-prune old TEST files, backups, PA or Viewer data.
Keep one service release plus one known-good rollback. The native data service
does not release historical build reservations or repair their filesystem.

Arbitrary backend handlers and the parked custom ModuleFrame capability bridge
remain separate from this reviewed native-data slice. They require their own
enforced runtime budgets and browser/capability acceptance. Do not describe
forms or a static preview as a completed general microservice host.
