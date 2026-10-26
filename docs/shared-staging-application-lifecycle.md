# Shared staging application lifecycle

The explicit owned queued-pilot services can compose the modular staging controller
and the container pilot against one `PilotOwner`, repository and work queue. This
is source composition, not automatic enrollment or a host deployment instruction.
Ordinary application construction retains its executor-conflict guard.

`prepare_shared_staging` derives the existing full `modular.` SHA-256 action keys
before the immutable shared policy registry is created. A missing or explicitly
disabled modular configuration retains the existing pilot-only behavior. An enabled
but invalid configuration fails closed and retains acquired resources for cleanup.
No preparation step makes a remote request or starts a claim loop.

The private and unified service factories install both controllers closed, then
attach one `SharedStagingLifecycle`. Authenticated HTTP is available while operator
session reconciliation is pending. The existing signed-human, CSRF-protected
acceptance route invokes recovery after acceptance. Acceptance alone does not
authorize an application deployment or replace independent task review.

Recovery checks both original operation sets before opening shared admission.
Outstanding, inconsistent or excessive history remains `recovery_required`.
Nothing is adopted, replayed, expired or released to make startup succeed. An
explicit retry can recheck the same retained original operations; there is no
background retry loop. The modular `auto_run` setting still controls its own loop.

Owner-only endpoints:

- `GET /api/hub/v1/staging/shared/status` returns bounded state/reason metadata.
- `POST /api/hub/v1/staging/shared/recover` accepts only an empty JSON object,
  requires a signed-in configured staging administrator and CSRF protection, and
  requests the existing composition's recovery. It grants no new permissions.

Shutdown first closes both claim paths synchronously. The outer server drains
accepted HTTP requests before full lifecycle closure; the lifecycle then drains
the actual captured execution/recovery/supervisor tasks. Only afterward does it
close the original modular transport. The service owns the pilot transport,
native event feed and owner, and releases them after their respective drains.
Rejected local factory tasks also drain before transport/owner release. A failed
drain retains the owner rather than claiming safe teardown.

Linux integration tests exercise real listeners, signed human sessions and real
ephemeral `PilotOwner` storage, with simulated executor responses. They do not
prove NAS storage ACLs, actual Docker effects, immutable deployment/rollback,
reboot recovery or a live owner dashboard. Inventory uses the separate private
HTTPS service described in `staging-runtime-inventory.md`; these routes do not
expose the Docker socket or replace its setup.
