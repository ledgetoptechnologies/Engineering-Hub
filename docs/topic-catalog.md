# Human-scoped topic discovery

`GET /api/hub/v1/topics` lists topic identifiers available to the authenticated
human, not all labels found in messages. Agent bearer credentials cannot use
this route. The fixed browser gateway allows only GET/HEAD, not mutations.

The default app has **no enumeration authority configured** and returns
`503 topic_catalog_unavailable`. This is distinct from an authoritative empty
scope (`200 {"items": []}`). A UI may retain explicitly labelled device-history
fallback; device history is not a membership grant or agent readiness signal.

An explicit trusted integration can inject `human_topics(principal)` into
`CanonicalRegistryBridge`, alongside its existing per-topic `human_reader`.
The callback must use the same authoritative human membership source and return
a `frozenset` of at most 500 canonical registry topic IDs. The bridge rechecks
each enumerated topic through the existing per-topic reader before returning
any of them. Missing readers, malformed scopes and unexpected resolver failures
fail closed. No wildcard reader, agent bearer substitution, background request,
message scan, new persisted registry or environment activation is supplied.

The response contains only sorted identifiers with `last_activity_at: null`
and `peer_count: null`. Membership alone establishes neither activity nor
available agents. No resolver credential or session binding is returned; the
response is marked `Cache-Control: no-store` and no cross-human cache is used.

This seam is not external collaborator onboarding. Thomas's project-scoped
human/assistant authorization, all other project API access checks and a real
membership-authority integration remain separate unfinished requirements.
Do not configure a global allowlist or infer grants from `body.topic` to make
this directory appear populated.

Tests cover disjoint principals, per-topic admission, bounds/malformed scopes,
redacted failures, human-only HTTP access and actual public/private gateway
projection using a synthetic verified Access identity. They do not prove live
Cloudflare, NAS, remote registry membership or correct-session agent wake.
