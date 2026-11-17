# Local pre-merge run

Hosted Actions run only on `main` and minutes are capped, so run everything
locally before merging. From Git Bash on the Windows development PC, in a
checkout of the commit you want to merge:

```bash
python scripts/local_premerge.py
```

It tests a `git archive` snapshot of `HEAD` (uncommitted edits are reported, not
tested): the full backend suite in WSL `Ubuntu-24.04` as root with venv
`/root/.venvs/eh-test`, the UI/DTO contract check, hub-ui `npm ci`/`npm test`/
`npm run build`, Playwright end-to-end, the control image build with the five
isolated container checks from `hub-control.yml`, and the pidfd launcher tests
inside `python:3.12-slim` (`scripts/hub_pidfd_slim_check.py`, also run by CI).
The backend step fails if any real-kernel pidfd test is skipped and prints the
skip count and top reasons. A step that cannot run, such as the Docker steps
with Docker Desktop stopped, is reported `NOT RUN` and the exit code is 2, never
a pass. Paste the printed Markdown summary into the PR body. Logs stay under
`%LOCALAPPDATA%\eh-premerge\<commit>-<id>\`; `--help` lists the options.

Not covered locally: GHCR push, the registry-digest re-test and `latest`
promotion, which only the main `publish` job performs.
