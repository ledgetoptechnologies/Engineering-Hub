# Main-only hosted checks and local development CI

Hosted CI belongs to `main`. The previous policy still ran ready pull requests
targeting main; the updated policy removes PR and tag triggers entirely. Main
pushes verify, and explicit Hub manual dispatches verify/publish only if the
selected ref is exactly `refs/heads/main`. Development branches do not consume
hosted runner minutes. The retired standalone bus publisher stays disabled;
this must not accidentally promote an old `internal-mcp:stable` image.

## Local validation, not automatic deployment

Development checks are explicit one-shot operations on the staging PC
`192.168.50.90`. The first supported project/profile is Engineering Hub Python
tests and UI unit tests/build. It is not an arbitrary workflow-YAML executor,
public self-hosted GitHub runner, webhook, always-on agent, Docker socket in
MCP, or automatic production deployment. Browser/container-release acceptance
and staged deployment/health/rollback are separate checks; a source-test report
must not be relabeled full CI/CD acceptance.

Only committed source is included. The selected development ref resolves to an
exact commit before creating an archive; dirty/untracked edits, `.git`, host
SSH credentials and live environment settings are not provided to test code.
The trusted runner selects fixed commands, not an arbitrary shell from an agent
request. The cached dependency image is pinned by immutable ID and lockfile
hashes; changed dependencies require deliberate reviewed cache rebuilding, not
network installation during the job.

Prepare that image with `git -c core.autocrlf=false -c core.eol=lf archive`,
not a Windows working-tree copy or an archive using implicit Git settings:
Git may store LF while the checkout uses CRLF. The branch/image lock hashes must
match the exact committed bytes. Base-cache compatibility permits only CRLF/LF
line-ending equivalence, not changed dependency declarations; the new image's
labels and bootstrap retain exact snapshot hashes.

## Existing-machine separation

Inventory found a live rootless pilot/recovery engine on the staging PC, with
18 retained containers and only about812MiB free on its dedicated volume. That
engine is **not** CI workspace. Do not reuse its socket, restart it, remove its
retained containers, or prune its images. The default rootful Docker engine
hosts Viewer/Project Alpha and is likewise not the development CI endpoint.

The separate CI engine is owned by the existing non-root staging user, with
private state under `~/engineering-hub-ci` and a Unix socket under
`/run/user/<uid>/engineering-hub-ci`. The on-demand transient user service must
have effective CPU, RAM, swap and task caps. The daemon is capped at one CPU,
2 GiB RAM, no swap and 384 tasks; the single serialized job independently has
one CPU, 2 GiB RAM, no swap and 256 tasks. These are separate budgets, not a
claim that a daemon service cgroup also contains Docker's container scopes.
Allow headroom for their combined worst case (two CPUs and 4 GiB RAM).
No root service, new
account, global permission change, linger or boot autostart is installed.
The live staging user's existing subuid/subgid ranges and CPU/memory/pids
delegation were observed; a short capped no-op user service completed cleanly.
Those observations alone are not an accepted image/test run.

The CI controller and daemon remain trusted code. Test code is a non-root
container with no network, ports, host Docker socket or secrets, read-only
root/source input, dropped capabilities and bounded disposable scratch. This
is not a claim of hardware isolation or safety against a hostile administrator
or kernel exploit. External/hostile multi-tenant code needs its own assessed
boundary; this is not automatic onboarding for outside agents.

## Repeatable operator/agent interface

Use the reviewed `scripts/staging_ci_engine.py` helper on the staging host:

```text
python3 scripts/staging_ci_engine.py inspect
python3 scripts/staging_ci_engine.py start
```

Retain its actual engine ID and service invocation ID. Do not substitute the
shared/default Docker context. The local test controller is:

```text
python3 scripts/local_development_ci.py --checkout <local-checkout> --ref refs/heads/<development-branch> --image sha256:<reviewed-ci-image-id> --engine unix://<private-ci-socket> --engine-id <observed-ci-engine-id> --output-root <private-ci-results> --profile all
```

The runner serializes its own jobs and retains reports/logs bound to the commit,
engine and image. A local file lock is not the Engineering Hub production-first
physical-machine queue. Do not run around an existing admitted production job
or claim that this helper preempts/reserves that job's machine. Headroom and
coordination still matter. An uncertain/nonterminal container outcome remains
held, not silently retried or treated as a free machine.

After accepted work is terminal, stop only the CI engine using the helper's
pinned engine/invocation arguments. Never stop another generation just because
its service name or socket path resembles this one. No global prune/cleanup.

## Readiness evidence

Source/unit tests, real engine startup/caps, loaded image identity and actual
branch checks must be reported separately. Root filesystem inventory had about
12GiB free; recheck before loading images. Do not use large client datasets or
the NAS staging share for CI scratch. No dataset moves, TrueNAS changes,
Cloudflare changes, registry publication or production promotion are included.

The final installed path, exact immutable image and actual test result are
recorded in the shared Hub note `operator-actions/local-development-ci.md` once
verified. If a gate fails, that note must say **not ready** rather than imply
that adding these scripts completed setup. Reboot leaves this on-demand:
there is no promise of unattended restart/recovery or automatic branch watching.
