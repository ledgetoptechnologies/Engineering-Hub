# Isolated task-tracking acceptance plan

Status: reviewable preparation, not a running environment or acceptance. This plan
covers project tracking and its independent review only. It does not activate,
replay, reconcile, or grant authority to any container pilot, executor or queue.
The exact release candidate and artifact must be selected and verified before use.

## Required inputs and stop conditions

- Record the full source commit, clean tree, exact build recipe, immutable backend
  image digest and matching UI build identity. Confirm the deployed MCP discovery,
  UI and database schema actually match that same candidate, including
  `hub_tracking_task_propose`, `hub_tracking_task_update`,
  `hub_tracking_task_list`, `hub_tracking_task_review`, and the human project
  tracking/review routes. Source tests or a successful build are not installed
  acceptance. Maintain a compatible rollback artifact and retain all state.
- Choose a task-owned isolated host, new private state root and unused ports.
  Measure free space, memory and I/O on that exact host immediately before use.
  Historical `192.168.50.90` observations in modular-staging-operations.md are
  stale; local Windows/WSL free space does not describe that shared host. Do not
  use a shared daemon, shared volumes or existing held TEST state as a shortcut.
- Review the full prerequisites in `agent-sessions-staging-service.md`. The
  checked-in compose template is explicitly not deploy-ready: it still needs a
  reviewed artifact, actual UID/filesystem acceptance, protected staging-only
  configuration and credentials, and a private same-origin HTTPS router and
  Access setup. Reuse an existing route only if its actual ownership and policy
  authorize this exact isolated backend, without altering shared services.
  Otherwise real connected acceptance is blocked until the route is supplied.
- The selected human must have a real, authenticated browser session on that
  exact staging origin; do not invent an actor or obtain credentials from another
  account or environment. An agent installation must be separately approved,
  and the owner must grant only the selected active project and required rights.
  This is independent of Cloudflare Access, agent connection approval, and any
  executor grant. Never put keys, headers, CSRF values, cookies, private evidence
  or client data into the acceptance record.

## Preparation and source-only test

Build and review the exact candidate and matching UI before scheduling a host
operation. The following is a source-test example, using a pre-existing isolated
Linux Python environment and disposable pytest data; adjust only the checked-out
path and Python executable. Run one bounded process, not shared Engine fixtures:

```sh
cd <reviewed-clean-checkout>
timeout 90 <isolated-python> -m pytest -q -p no:cacheprovider \
  tests/test_session_hub_bridge.py::test_tracking_review_requires_independent_agent_family \
  tests/test_session_hub_bridge.py::test_tracking_review_serializes_with_session_revocation \
  tests/test_session_hub_bridge.py::test_tracking_review_revoked_session_cannot_mutate_claim \
  tests/test_hub_api.py::test_human_tracking_review_csrf_and_competing_revision \
  tests/test_hub_mcp.py::test_tracking_task_idempotency_evidence_and_no_execution_authority
```

These tests use synthetic identity and data. They check contracts, not a live
owner, actual Access routing, client setup, exact chat continuity or delivery.
Do not treat this as the real-chat gate.

## Isolated service composition, only after prerequisites

Use the selected immutable image and reviewed deployment configuration. The
existing initializer creates a **new, nonexistent child** of an existing private
parent; it cannot adopt an existing directory or a live ledger:

```sh
python -m engineering_hub.staging_state_init --new-state-dir <new-private-child>
python -m engineering_hub.owned_inactive_hub --config <isolated-settings.json>
```

These are entrypoint shapes, not a complete launch recipe. The second command
uses the image environment and paired agent/browser listeners; source defaults
bind container ports 8770 and 8771 on all container interfaces. Do not run it
unisolated on a shared host or assume it is loopback-only. Use independently
verified isolated port mappings, no host network, no Engine socket or executor,
no shared mounts, bounded resources and a task-owned lifecycle. Follow the
initializer and paired-listener conditions in the staging service document.
This plan creates no settings, state, credentials, router or running service.

## Human, installation and grant

1. Sign in through the isolated staging HTTPS browser route. Verify the resulting
   authenticated owner and the backend/UI identity from that exact browser
   session. Do not expose cookies or CSRF values.
2. If no suitable isolated installation exists, use the reviewed helper with a
   new private staging-only profile. Run `enroll` and compare the public pairing
   code and fingerprint with **Agents → Installations** before the owner approves
   it. Do not overwrite another profile or copy a production secret. See
   `revocable-agent-staging.md` for the exact helper arguments and protection.
3. The owner separately sets a finite grant for the exact active project:
   `tracking.propose`, `tracking.update`, `tracking.read`. Add `task.list` only
   if assigned-task continuity is also required. An independent agent-family
   reviewer needs a separately approved installation and `tracking.read` plus
   `tracking.review`; a human review uses the signed-in browser with Origin and
   CSRF protection. A grant replacement uses the current revision and retains
   unrelated rights only when explicitly intended. Verify effective grants and
   project scope; never silently broaden an existing grant.
4. Connect a fresh named helper session on the target client and install/select
   its private headers helper through the supported client path. The helper's
   header output contains a bearer and must not be logged. Sessions last at most
   15 minutes; reconnect deliberately after expiry. Preserve existing connectors.

## Acceptance in the actual chat

- In the exact intended chat, confirm the loaded staging connector and fresh
  `session_identity`; inspect only nonsecret installation/session/family IDs.
  Identity establishes that session, not an independently authenticated desktop
  chat ID. A CLI invocation, another chat, connector listing, subscription, or a
  stored label is not proof of delivery to the intended chat.
- Create a clearly synthetic inert tracking record in the selected test project,
  add a progress update and a public nonsecret evidence link, and read it through
  the staging MCP and the human project Work → Project tracking section. Verify
  project scope and that it is distinct from executable tasks. The claim must be
  displayed as **completion claimed — unverified** until reviewed.
- In a subsequent actual turn of the same chat, request its continuation and
  confirm that the exact record, progress and evidence are retrieved with matching
  nonsecret IDs. Do not infer this from a response generated within the first
  turn, a synthetic test, a new chat or a process being alive. Test an ungranted
  project denial and session revocation without printing credentials.
- If reviewing, use the authenticated human or a separately granted different
  agent family to inspect the exact claim and evidence. Record the reviewed
  revision and resulting state; reject same-family self-review, stale revisions
  and missing or mismatched evidence. Review is an attributed claim that the
  reviewer inspected evidence, not proof of execution or authorization.
- Record separately: exact source/artifact and UI identity; target chat's fresh
  session identity and each actual turn; project, task ID, revisions, sanitized
  outcomes, denied checks, human review and cleanup. Record what was not tested.
  No claim of native automatic wake is justified by ordinary MCP reads.

## Shutdown and retention

Track only processes, ports, temporary profiles and resources created for this
acceptance. Stop admission and cooperatively drain the owned service, verify its
processes exit, and preserve isolated databases/audit and any ambiguous state
according to the approved retention plan. Do not prune, terminate, reset, remove
or reuse shared processes, profiles, databases, journals or containers. A stop,
empty scope, healthy endpoint, or synthetic success does not resolve any held
external effect. If isolation, capacity, routing, owner sign-in or the exact
chat connector cannot be established, report the specific gate and leave the
real acceptance unverified.
