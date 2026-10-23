# Exact queued-session acceptance attachment — source helper only

`attach_queued_session_acceptance` is an explicit pre-serving composition helper,
not default startup, a configuration flag, operator enrollment or live activation.
It attaches the existing bounded browser acceptance routes to a real queued Hub
core only after checking the same repository, owner, target, runtime and exact
`StartupReconciliation` object. Accepting an unrelated gate cannot unblock this
queue. The broker exposes `assert_binding` for a trusted service handover without
reading private broker fields or mistaking an object type for a capability match.

The caller supplies an independently enrolled operator actor, retained report and
evidence, and synchronous durable audit callback. There are no permissive defaults,
request-provided identities, invented inventory statements, persisted accepted
bits, environment/CLI selectors or new credentials. Thomas or another allowed
human is not automatically an operator. All authentication/CSRF, bounded parser,
deadline and audit limitations of `session-acceptance-channel.md` remain applicable.

## Explicit source service hook

The private/unified queued source compositions accept an optional explicit
`session_acceptance_factory(core, owner, repo, target, reconciliation)` Python
dependency. No callback means no attachment; a pending gate remains unable to
execute. Each pair factory calls it once after `create_app` and before
`HubFrontends`, passing the exact already owned capabilities. The trusted factory
can call this helper using independently enrolled inputs. The service checks
the exact attached broker type and public `assert_binding`. Async results are
rejected; returned local Tasks/Futures are cancelled and drained before owner
cleanup, never silently adopted as authority. Current/lifecycle tasks are not
cancelled or self-awaited. Invalid returns and unreturned resources remain the
trusted factory's responsibility. Neither helper nor hook is a source of operator
identity, report facts, audit durability or current-host trust.

Keep route acceptance and queued admissions on the same owned event loop; no
await occurs between gate opening and result audit. Do not treat the broker lock
as a cross-thread effect barrier. On shutdown, close runtime admission (which
closes this same gate), retain/drain already accepted operations and listeners,
then close the broker and accepted client before releasing the owner. Broker
close only revokes future admission; it does not close the owner, stop a remote
effect, or authorize replay/recovery. A failed pre-transfer factory still owns
its unaccepted inputs/resources.

The private browser gateway admits only the exact session GET/POST route; the
backend still checks real human authentication, CSRF and the pinned operator.
No wildcard, report uploader, agent endpoint or extra header trust is added.
No default startup selects the hook. No ordinary app, TrueNAS mapping,
production deployment, subscription, access policy, live identity or machine is
changed. Tests use real Linux ownership, native queue events, actual signed
synthetic Cloudflare sessions and typed fake executor responses. Their success is
not proof of live inventory, exclusive writers, application deployment, protected
restore or reboot acceptance.
