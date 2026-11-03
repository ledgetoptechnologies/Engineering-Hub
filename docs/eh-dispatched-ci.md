# Running CI through Engineering Hub instead of hosted minutes

Status: proposal for review. Nothing here registers a runner, changes a host,
creates a credential or changes billing. Each of those needs Beau.

## Where we are (2026-11-03)

- Hosted Actions has not run since 2026-10-30 21:16Z. Every job since then
  fails before starting with "recent account payments have failed or your
  spending limit needs to be increased". Only the account owner can fix that.
- Hosted CI is already main-only ([local-development-ci.md](local-development-ci.md)).
  Each main merge ran two workflows: `container.yml` (about 9 minutes) and
  `hub-control.yml` (16 to 24 minutes). About 112 commits reached main in
  October; the two workflows logged 194 and 170 runs (some before PR triggers
  were removed).
- Both workflows ran the same full Python suite. This change removes the copy
  in `container.yml`, which keeps Compose validation and the root image
  build. That saves about 7 of every 30 hosted minutes per merge without
  changing triggers, publication order or any image identity.
- Docs-only merges are rare (2 of 112 in October), and both Dockerfiles copy
  `README.md`, so path filters would save little and risk skipping a real
  build input. They are deliberately not added.

## What already exists

- `scripts/local_development_ci.py` runs the Python and UI suites for an
  exact committed ref on the staging PC, in a separate capped rootless engine
  with no network, secrets or host socket for test code.
- `QueuedDevelopmentCI` (source only) binds such a run to the Hub's machine
  queue with durable attempts, lease renewal and terminal verification.
- Missing: an enrolled host provider and transport, a trusted record of the
  result on the Hub task, and anything that tells GitHub or a reviewer the
  result.

## Options

1. **Hub-launched just-in-time runners (recommended).** Workflows that should
   stay off hosted minutes use `runs-on: [self-hosted, eh-ci]`. When GitHub
   queues such a job, the Hub admits a CI job through its own queue
   (production work keeps precedence), asks GitHub for a single-use
   just-in-time runner configuration, and starts one ephemeral runner inside
   the existing capped CI engine. The runner takes exactly one job and exits;
   the Hub records the outcome on the task. This is what "dynamic" means here:
   nothing runs or listens between jobs, and the same workflow YAML runs on a
   hosted runner by changing one label.
   - Needs Beau: a GitHub App (or fine-grained token) limited to this
     repository's self-hosted runner administration, stored only on the Hub
     host; and approval to start runner processes in the staging CI engine.
   - Risk: workflow code from anyone who can push to the repository runs on
     the staging PC. The repository is private, the runner is single-use and
     unprivileged, and the CI engine has no network for test containers, but
     the runner process itself needs network to reach GitHub. Keep it out of
     the pilot engine and the default Docker engine.
2. **Hub-dispatched local CI only.** Finish the `QueuedDevelopmentCI`
   provider and run `local_development_ci.py` for each reviewed task commit;
   show the result on the task and optionally post a commit status. No GitHub
   runner at all, but workflow YAML and Hub CI profiles must be kept in step
   by hand, and publication to GHCR still needs hosted Actions or a separate
   trusted publisher.
3. **Stay hosted, spend less.** Fix billing, set a spending limit, and keep
   trimming duplicate work. Cheapest to do, but CI stops again whenever the
   limit is hit.

## Proposed order

1. Beau restores hosted Actions billing or sets a limit (unblocks main CI now).
2. Merge this change (one fewer full suite per merge).
3. Codex finishes the `QueuedDevelopmentCI` host provider, which both option
   1 and option 2 need: admission, lease renewal and terminal verification on
   the staging CI engine.
4. Beau decides between options 1 and 2 through a Hub decision, including the
   credential and host approvals above.
5. Move `hub-control.yml` verify to the chosen path. Keep publication on
   hosted runners until a trusted build record binds source commit to image
   config ID; the release-evidence view already says CI is not verified there.

Agents keep running the suites locally before review, as today. A local run is
not a CI result for release purposes.
