# Staging observer: proposed host acceptance checklist

Status: **preparation only; not authorization, installation, or acceptance**.
This checklist proposes connecting the snapshot-only observer to the existing Hub.
All host-specific addresses, paths, identities, ports and artifact digests remain
unset until measured and reviewed. The existing proxy draft remains **BLOCKED**;
see [its unresolved boundaries](staging-readonly-observer.md#inert-private-proxy-example).

The collector reads the explicitly selected ordinary Docker endpoint and systemd;
the non-root observer reads only its protected snapshot and does not receive a
Docker socket. The original protected TEST Engine, socket, storage and hold are
separate: do not contact, adopt, restore, release or clean them as part of this
observer. This visibility path does not admit jobs or prove execution readiness.
Existing production services and data must be preserved.

## Prepare and verify inputs

- [ ] Independently review and select an immutable published Hub and staging
  wheel; record exact source revision, artifact digest and installed identities.
  A source test or local wheel is not proof of the installed build.
- [ ] Measure the intended ordinary host Engine/socket and storage, service
  identity, snapshot parent and all ancestors, spare distinct ports, actual NAS
  network source, and resource headroom. Verify proposed paths and mounts are
  disjoint from protected TEST and unrelated workloads. Do not infer physical
  isolation or writer exclusion from a different Engine ID.
- [ ] Select a dedicated non-root observer UID/GID. Require and verify that the
  observer has no supplementary groups beyond its effective primary GID, clear capabilities, and no effective
  access to any Docker socket; define bounded process and connection resources.
  This applies to the snapshot-only observer, not the separate collector that
  reads its explicitly selected Docker endpoint. The effective-UID guard alone
  does not prove isolation.
- [ ] Resolve collector reliability before relying on it: bounded one-shot
  operator observations produced both observed and unavailable results, including
  a Docker-capture failure. The credential-free receipts are in the Engineering
  Hub shared note `reviews/staging-collector-one-shot-20261030.md`; these were
  host observations, not repository test results. Identify and correct the
  cause, establish a bounded
  refresh and monitoring approach, assign an accountable human owner for its
  freshness checks and stale/unavailable response, and define the check cadence
  and freshness threshold. Verify stale/unavailable behavior. Publication exit
  zero alone does not mean observation succeeded. No scheduler is supplied by
  the source.
- [ ] Resolve and independently test the blocked proxy boundaries: duplicate
  Authorization handling, error and startup logging, actual TCP peer semantics,
  pre-header/TLS connection admission, source restriction and effective firewall
  rules. Review binary/package provenance, exact config and resource limits.
  A syntax check or synthetic test alone is not live acceptance.
- [ ] Define a dedicated private CA/certificate with the actual literal-IP SAN,
  dedicated observer bearer and exact authorized human administrator IDs through
  approved secret-handling procedures. Do not put secret values in this checklist,
  logs, source, receipts or proxy configuration.

## Staging host preflight and acceptance

After the applicable owner scope is authorized, the staging operator verifies the
installed immutable artifact and independently checks the selected host inputs.
Use the root-owned collector output under protected, root-owned, non-writable
ancestors. Run `observer_preflight` as the proposed non-root identity with the
actual protected snapshot and token references. Require identity, snapshot and
credential checks all `observed`; any unavailable, stale, invalid or mismatched
result blocks activation. Preflight is point-in-time evidence, not a substitute
for ACL, supplementary-group, capability, service-unit or firewall review.

Before accepting a route, test the exact reviewed proxy configuration and TLS
material in isolation, then the actual private path. Verify allowed and denied
TCP source, pinned CA and IP SAN, correct and wrong bearer, duplicate headers,
method/path/query variants, request bodies, redirects, incomplete connections,
stale or unavailable upstream, and bounded resource behavior. Confirm logs and
stderr do not expose request or credential material. No public tunnel is used.

## Beau's NAS-side work

Beau manages the NAS. After reviewing the exact scoped plan, Beau places or
mounts the dedicated CA and bearer into the existing Hub deployment with the
necessary protected permissions and configures the optional
`staging-observer.json` alongside the ordinary staging configuration. Its
schema, enabled state, private literal-IP HTTPS origin, protected CA and bearer
file references, and exact human administrator IDs must be reviewed against the
installed Hub contract. Do not copy agent tokens, alter the existing Hub data
root, or assume an unverified NAS path or permission. Beau performs any needed
NAS app update or restart under the separately agreed scope.

Verify the installed Hub build, authenticated human and administrator access,
actual private route and fresh snapshot through the Hub. Deny an unauthorized
actor and verify wrong CA/bearer and stale/unavailable results fail closed.
Exercise authorization with a real authenticated human session whose exact
identity is in the reviewed `admin_actors` list, then with an authenticated
human outside that list; verify the former is allowed and the latter denied.
Synthetic sessions do not establish actual identity-provider mapping or actor
authorization.
Record artifact identities, timestamps, coarse states and test outcomes without
names, credentials or raw telemetry. A source fixture does not prove NAS route,
actual peer identity, reboot recovery or protected Engine reconciliation.

Before activation, document the approved expected boot behavior for each new
component and the human owner responsible for checking freshness after restart.
No autostart or reboot action is implied here. Only after separate explicit
authorization, verify the actual post-reboot state: only approved components
start, the snapshot refreshes within its defined freshness threshold, stale or
unavailable data fails closed, and no job, replay, or protected TEST resource
action occurs. Source and container-restart fixtures are not host-reboot
acceptance.

## Rollback

Before activation, record the exact existing configuration and ownership of each
new component and define who can stop it. If a check fails, disable the new Hub
observer configuration and stop only the newly owned observer/proxy/collector
components; reverse only any specifically approved, newly owned firewall rule.
Preserve prior Hub data, existing services, credentials and protected TEST state.
Verify the Hub remains healthy and the observer reports unavailable after
rollback. Do not prune, force-stop, reset a hold or remove an unverified resource.
