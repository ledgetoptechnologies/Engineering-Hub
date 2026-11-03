# Draft: Cloudflare-bound GitHub sign-in

This is an experimental opt-in for the shared-root Hub using the verified split
browser frontend. It is not a release or deployment instruction. The existing
version 1 and 2 settings continue to select their existing sign-in behavior.

Version 3 explicitly selects a storage layout and the `github_cloudflare`
human-auth mode. The Hub verifies the Cloudflare Access assertion at the browser
front door and requires the exact enrolled issuer, audience and subject again at
the Hub auth routes. GitHub OAuth verifies the enrolled numeric GitHub account.
The resulting human principal is the existing Cloudflare-derived actor; this
avoids silently assigning existing owner permissions to a new principal.

Every human request needs a currently valid Access assertion for the enrolled
identity. The Hub GitHub session has a fixed local lifetime (normally eight
hours) and can be reused with a later valid Access assertion for the same
identity until the Hub session expires or is revoked. Signing out revokes that
local session; separate sessions are not implicitly revoked. Provider-side
revocation is subject to the validity and revocation behavior of the Access
assertion; the Hub does not promise instantaneous upstream revocation.

The standalone core has no verified browser ingress capability and cannot
establish or use a bound human session. Its fixed application shell may still
be served without a human session under existing combined-mode behavior.

The protected-ledger and owned-pilot entrypoints deliberately reject this mode.
A version 3 protected storage selection sets `protected_ledger` but is not
sufficient to make those entrypoints accept the new mode. Do not treat settings
validation as a protected deployment or an automatic storage migration.

The tests use synthetic Access keys and a fake GitHub provider. Real OAuth app
configuration, identity enrollment, client secret provisioning, Cloudflare
policy, deployed frontend behavior, rollout and recovery remain separate work.
