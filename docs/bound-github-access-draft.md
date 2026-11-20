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

The owned inactive entrypoint accepts this mode only after explicit offline
preparation of the bound-auth schema under the existing PilotOwner. Caller
attestations that writers stopped and a backup exists are not independent proof
of either condition. This operation is not called by the server and does not
repair partially present tables. Owned-pilot draft factories accept the mode;
the private browser gateway also needs an explicit callback opt-in on its public
side. A version 3 protected selection is not automatic migration, a completed
host validation, or permission to start an owned-pilot runtime.

The tests use synthetic Access keys and a fake GitHub provider. Real OAuth app
configuration, identity enrollment, client secret provisioning, Cloudflare
policy, deployed frontend behavior, rollout and recovery remain separate work.

For the separate standalone public gateway, `--bound-github-callback` is an
independent, default-off routing flag. The private service must independently
select bound mode; if either side does not opt in, the callback is denied. The
flag does not validate Cloudflare, identify a backend, authorize a user, or
establish executor peer binding. The private browser ingress verifies Access and
the auth flow independently verifies the bound identity and GitHub account.
