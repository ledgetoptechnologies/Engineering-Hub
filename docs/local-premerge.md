# Local pre-merge run

Hosted Actions run only on `main` and minutes are capped, so run everything
locally before merging. From Git Bash on the Windows development PC, in a
checkout of the commit you want to merge:

```bash
python scripts/local_premerge.py
```

It tests an LF `git archive` snapshot of `HEAD` (uncommitted edits are reported,
not tested), importing that snapshot through `PYTHONPATH` rather than an editable
install in the shared venv: the full backend suite in WSL `Ubuntu-24.04` as root with venv
`/root/.venvs/eh-test`, the UI/DTO contract check, hub-ui `npm ci`/`npm test`/
`npm run build`, Playwright end-to-end, the control image build with the five
isolated container checks from `hub-control.yml`, and the pidfd launcher tests
inside `python:3.12-slim` (`scripts/hub_pidfd_slim_check.py`, also run by CI).
The backend step fails if any real-kernel pidfd test is skipped and prints the
skip count and top reasons. A step that cannot run, such as the Docker steps
with Docker Desktop stopped, is reported `NOT RUN` and the exit code is 2, never
a pass. Steps run one at a time with one Playwright worker and the pidfd
container capped at 2 CPUs and 4 GiB (the five container checks cap themselves
at 1 CPU and 512 MiB; BuildKit image builds take no limits); `--e2e-workers`,
`--docker-cpus` and `--docker-memory` raise them. Paste the printed Markdown
summary into the PR body.

The run cleans up only what it started (`scripts/owned_cleanup.py`). Each local
process tree runs in its own Windows job object, each WSL step in its own Linux
session, and each container carries the run's owner label or its unique image
tag. After every step, and on a timeout or Ctrl+C, the run stops those and
checks they are gone. A timeout or Ctrl+C stops the run, so later steps never
start. If something cannot be proven gone, the run exits 3 and lists it.
Exit codes: 0 pass, 1 fail or timeout, 2 something not run, 3 cleanup not
proven, 130 Ctrl+C. `python scripts/local_premerge.py --self-test` exercises
the timeout and Ctrl+C paths with stand-in steps (about a minute). Logs stay under
`~/.eh-premerge/<commit>-<id>/`; `--help` lists the options.

Not covered locally: GHCR push, the registry-digest re-test and `latest`
promotion, which only the main `publish` job performs.
