"""One-command local pre-merge run for the current commit (Windows + WSL + Docker).

Run from Git Bash in a checkout:  python scripts/local_premerge.py

Every step works on an LF `git archive` snapshot of the exact commit, never on
uncommitted files. Steps run one at a time (see docs/local-premerge.md):
  backend   full pytest in WSL (default Ubuntu-24.04, root, venv /root/.venvs/eh-test)
            with skip accounting; FAILS if any real-kernel pidfd launcher test
            (tests/test_hub_container_executor_launch_supervisor.py) is skipped
  contract  scripts/check_hub_ui_contract.py (WSL Python, Windows node via interop)
  ui        hub-ui npm ci, npm test, npm run build (Windows)
  e2e       Playwright end-to-end (Windows, Edge channel per playwright.config.ts)
  image     docker build docker/Dockerfile.hub-control with VCS_REF=<commit>
  smokes    the five isolated container checks hub-control.yml runs on the candidate
  pidfd-slim  the pidfd module inside python:3.12-slim (same script CI runs)

Ownership and cleanup (scripts/owned_cleanup.py): every local process tree runs
in its own Windows job object, every WSL step in its own Linux session, and
every container carries an owned label or the run's unique image tag. After
each step, on timeout and on Ctrl+C, the run stops what it owns and proves it
is gone. A timeout or Ctrl+C stops the run; later steps do not start. Nothing
the run did not start is touched.

Exit codes: 0 all passed; 1 a step failed or timed out; 2 nothing failed but a
step was NOT RUN; 3 cleanup could not be proven (lists what is left); 130 Ctrl+C.
`--self-test` exercises the timeout and cancel cleanup paths with stand-in steps.
"""
from __future__ import annotations

import argparse
import collections
import os
from pathlib import Path, PureWindowsPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
from owned_cleanup import (Cancelled, CleanupError, StepTimeout, WINDOWS, interrupt_main_after,  # noqa: E402
                           owned_containers, pid_alive, remove_containers, run_owned)

REPO = Path(__file__).resolve().parents[1]
PIDFD_MODULE = 'test_hub_container_executor_launch_supervisor'
SMOKES = ('hub_release_smoke', 'hub_split_container_smoke', 'hub_unified_container_smoke',
          'hub_settings_container_smoke', 'hub_protected_container_smoke')
PIDFD_LABEL = 'eh.pidfd-check.owner'
SMOKE_VOLUME_LABEL = 'hub.release-owner'
PASS, FAIL, NOT_RUN = 'PASS', 'FAIL', 'NOT RUN'


def say(message):
    print(message, flush=True)


def fmt(seconds):
    if seconds is None:
        return '-'
    minutes, secs = divmod(int(round(seconds)), 60)
    return f'{minutes}m{secs:02d}s' if minutes else f'{secs}s'


class StopRun(Exception):
    """Stop the run: no later step starts."""

    def __init__(self, reason, kind):
        super().__init__(reason)
        self.reason, self.kind = reason, kind  # kind: timeout | cancelled | cleanup


class Step:
    def __init__(self, name, label):
        self.name, self.label = name, label
        self.result, self.seconds, self.detail = NOT_RUN, None, 'not started'


class Run:
    def __init__(self, args, work):
        self.args = args
        self.steps = collections.OrderedDict()
        self.logs = Path(work)
        self.leftovers = []
        self.stopped = None

    def step(self, name, label):
        self.steps[name] = Step(name, label)
        return self.steps[name]

    def read_log(self, name):
        log = self.logs / f'{name}.log'
        return log.read_text(encoding='utf-8', errors='replace') if log.exists() else ''

    def execute(self, step, argv, *, cwd=None, env=None, timeout=1800, after=None):
        """Run argv as an owned process tree; return (ok, output).

        Raises StopRun after a timeout or Ctrl+C (owned work already stopped
        and verified gone) or when cleanup cannot be proven.
        """
        try:
            code, output = run_owned(argv, log_path=self.logs / f'{step.name}.log', timeout=timeout,
                                     cwd=cwd, env=env, after=after)
        except CleanupError as error:
            self.leftovers += error.leftovers
            raise StopRun(f'cleanup not proven after {step.label}', 'cleanup') from None
        except StepTimeout as error:
            raise StopRun(f'timed out ({error}); owned processes and containers stopped and verified gone',
                          'timeout') from None
        except Cancelled:
            raise StopRun('cancelled (Ctrl+C); owned processes and containers stopped and verified gone',
                          'cancelled') from None
        return code == 0, output

    def timed(self, step, func):
        if self.stopped:
            step.detail = 'not started: run stopped'
            return False
        say(f'-> {step.label}')
        start = time.monotonic()
        try:
            result, detail = func()
        except StopRun as stop:
            step.result, step.detail, step.seconds = FAIL, stop.reason, time.monotonic() - start
            self.stopped = stop
            say(f'   {step.label}: FAIL ({fmt(step.seconds)}) {stop.reason}')
            raise
        except Exception as error:  # report, never crash the summary
            result, detail = FAIL, f'{type(error).__name__}: {error}'
        step.result, step.detail = result, detail
        step.seconds = time.monotonic() - start if result != NOT_RUN else None
        say(f'   {step.label}: {result} ({fmt(step.seconds)}) {detail}')
        if result == FAIL:
            tail = self.read_log(step.name).splitlines()[-25:]
            if tail:
                say('   --- last log lines ---\n   ' + '\n   '.join(tail))
        return result == PASS


def git(*args):
    return subprocess.run(['git', '-C', str(REPO), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def to_wsl(path):
    win = PureWindowsPath(str(Path(path).resolve()))
    drive = win.drive.rstrip(':').lower()
    return '/mnt/' + drive + '/' + '/'.join(win.parts[1:])


def windows_env(ui_dir=None):
    """A short PATH: Git Bash PATHs can exceed what cmd.exe passes to children."""
    env = dict(os.environ)
    first = []
    for tool in ('node', 'npm', 'git', 'docker'):
        found = shutil.which(tool)
        if found:
            first.append(str(Path(found).parent))
    if ui_dir:
        first.insert(0, str(Path(ui_dir) / 'node_modules' / '.bin'))
    system_root = os.environ.get('SystemRoot', r'C:\WINDOWS')
    first += [system_root + r'\System32', system_root, system_root + r'\System32\Wbem']
    seen, ordered = set(), []
    for entry in first + env.get('PATH', '').split(os.pathsep):
        key = entry.lower().rstrip('\\/')
        if entry and key not in seen:
            seen.add(key)
            ordered.append(entry)
    kept, size = [], 0
    for entry in ordered:  # npm 12 run-script breaks when PATH nears 8k chars
        if size + len(entry) + 1 > 4000:
            break
        kept.append(entry)
        size += len(entry) + 1
    env['PATH'] = os.pathsep.join(kept)
    return env


# ---------------------------------------------------------------- WSL
BACKEND_SCRIPT = r'''set -euo pipefail
D="__DIR__"
rm -rf "$D"; mkdir -p "$D/src" "$D/bin" "$D/tmp"
tar -xf "__TAR__" -C "$D/src"
. "__VENV__/bin/activate"
# No pip install -e: the venv is shared, so an editable install from another
# tree (or a concurrent run) could leak into subprocesses. Pin this snapshot.
export PYTHONPATH="$D/src/src:$D/src"
cd "$D/src"
python -c "import os,sys,engineering_hub; print('EH_PY', sys.version.split()[0], 'pidfd_open=%s' % hasattr(os,'pidfd_open'), 'uid=%d' % os.getuid()); assert engineering_hub.__file__.startswith(os.environ['PWD'] + '/src/'), 'engineering_hub imported from ' + engineering_hub.__file__"
python -W default -m pytest -q -p no:cacheprovider --basetemp "$D/tmp" -rs --junitxml "__JUNIT__"
'''

CONTRACT_SCRIPT = r'''set -euo pipefail
D="__DIR__"
mkdir -p "$D/bin"
NODE="$(command -v node || command -v node.exe || echo '/mnt/c/Program Files/nodejs/node.exe')"
ln -sf "$NODE" "$D/bin/node"
. "__VENV__/bin/activate"
export PYTHONPATH="$D/src/src:$D/src"
export PATH="$D/bin:$PATH"
cd "__WINSRC__"
"$D/bin/node" --version
python -B scripts/check_hub_ui_contract.py
'''

# Runs as the session leader created by `setsid --wait`: $$ is the session id.
LAUNCHER = 'mkdir -p "$(dirname "__SID__")"; echo "$$" > "__SID__"; exec bash "__SCRIPT__"\n'

SESSION_CLEANUP = r'''f="__SID__"
[ -f "$f" ] || { echo NOFILE; exit 0; }
s=$(cat "$f")
case "$s" in ''|*[!0-9]*) echo "BADSID $s"; exit 0;; esac
[ "$s" -gt 1 ] || { echo "BADSID $s"; exit 0; }
if pgrep -s "$s" >/dev/null; then
  pkill -TERM -s "$s" || true
  for i in $(seq 50); do pgrep -s "$s" >/dev/null || break; sleep 0.1; done
  pkill -KILL -s "$s" || true
  for i in $(seq 50); do pgrep -s "$s" >/dev/null || break; sleep 0.1; done
fi
left=$(pgrep -s "$s" | tr '\n' ' ')
if [ -z "$left" ]; then rm -f "$f"; echo GONE; else echo "LEFT $left"; fi
'''


def wsl_call(run, argv, timeout=120):
    return subprocess.run(['wsl', '-d', run.args.wsl_distro, '-u', 'root', '--exec', *argv],
                          capture_output=True, text=True, encoding='utf-8', errors='replace',
                          env=dict(os.environ, MSYS_NO_PATHCONV='1'), timeout=timeout)


def wsl_session_cleanup(run, name, sid_file):
    path = run.logs / f'{name}.cleanup.sh'
    path.write_text(SESSION_CLEANUP.replace('__SID__', sid_file), encoding='utf-8', newline='\n')
    try:
        result = wsl_call(run, ['bash', to_wsl(path)], timeout=60)
    except subprocess.TimeoutExpired:
        return [f'WSL session cleanup for {name} timed out']
    lines = result.stdout.strip().splitlines()
    status = lines[-1] if lines else f'no output (exit {result.returncode})'
    if status in ('GONE', 'NOFILE'):
        return []
    return [f'WSL session of {name}: {status}']


def wsl(run, step, script_text, timeout):
    script = run.logs / f'{step.name}.sh'
    script.write_text(script_text, encoding='utf-8', newline='\n')
    sid_file = f'/root/eh-premerge/{run.nonce}-{step.name}.sid'
    launcher = run.logs / f'{step.name}.launch.sh'
    launcher.write_text(LAUNCHER.replace('__SID__', sid_file).replace('__SCRIPT__', to_wsl(script)),
                        encoding='utf-8', newline='\n')
    argv = ['wsl', '-d', run.args.wsl_distro, '-u', 'root', '--exec', 'setsid', '--wait', 'bash',
            to_wsl(launcher)]
    return run.execute(step, argv, env=dict(os.environ, MSYS_NO_PATHCONV='1'), timeout=timeout,
                       after=lambda killed: wsl_session_cleanup(run, step.name, sid_file))


def junit_summary(path):
    cases = list(ET.parse(path).iter('testcase'))
    counts = collections.Counter()
    reasons = collections.Counter()
    pidfd = collections.Counter()
    for case in cases:
        kind = 'passed'
        for child in case:
            if child.tag in ('failure', 'error'):
                kind = 'failed'
                break
            if child.tag == 'skipped':
                kind = 'skipped'
                message = re.sub(r'\s+', ' ', child.get('message') or 'skipped')
                reasons[message[:110]] += 1
        counts[kind] += 1
        if PIDFD_MODULE in (case.get('classname') or ''):
            pidfd[kind] += 1
    return counts, reasons, pidfd


def backend(run, wsl_dir, tar):
    step = run.steps['backend']
    junit = run.logs / 'backend-junit.xml'

    def go():
        if not run.args.wsl_distro or shutil.which('wsl') is None:
            return NOT_RUN, 'wsl.exe not available'
        script = (BACKEND_SCRIPT.replace('__DIR__', wsl_dir).replace('__TAR__', to_wsl(tar))
                  .replace('__VENV__', run.args.wsl_venv).replace('__JUNIT__', to_wsl(junit)))
        ok, output = wsl(run, step, script, timeout=run.args.backend_timeout)
        if not junit.exists():
            return FAIL, 'pytest produced no JUnit report (setup failed; see log)'
        counts, reasons, pidfd = junit_summary(junit)
        run.skip_reasons = reasons
        python = re.search(r'EH_PY (\S+) pidfd_open=(\w+) uid=(\d+)', output)
        run.backend_python = python.groups() if python else None
        detail = (f"{counts['passed']} passed, {counts['skipped']} skipped, {counts['failed']} failed; "
                  f"pidfd module: {pidfd['passed']} passed, {pidfd['skipped']} skipped")
        if not ok or counts['failed']:
            return FAIL, detail
        if pidfd['skipped'] or not pidfd['passed']:
            return FAIL, detail + ' -- real-kernel pidfd tests did not all run'
        return PASS, detail

    run.timed(step, go)
    contract = run.steps['contract']
    if step.result == NOT_RUN:
        contract.detail = 'needs the WSL backend environment'
        return

    def check_contract():
        script = (CONTRACT_SCRIPT.replace('__DIR__', wsl_dir).replace('__VENV__', run.args.wsl_venv)
                  .replace('__WINSRC__', to_wsl(run.src)))
        ok, output = wsl(run, contract, script, timeout=300)
        lines = [l for l in output.strip().splitlines() if l.strip() and not l.startswith('[')]
        last = (lines or ['no output'])[-1]
        return (PASS if ok else FAIL), last[:120]

    run.timed(contract, check_contract)


# ---------------------------------------------------------------- Windows side
def ui_and_docker(run):
    ui_dir = run.src / 'hub-ui'
    env = windows_env(ui_dir)
    npm = shutil.which('npm', path=env['PATH'])
    if not npm:
        for name in ('ui', 'e2e'):
            run.steps[name].detail = 'npm not found'
    else:
        def ui():
            for argv in ([npm, 'ci', '--ignore-scripts'], [npm, 'test'], [npm, 'run', 'build']):
                ok, _ = run.execute(run.steps['ui'], argv, cwd=ui_dir, env=env, timeout=900)
                if not ok:
                    return FAIL, ' '.join(argv[1:]) + ' failed'
            tests = re.findall(r'Tests\s+(?:\x1b\[[0-9;]*m)*\s*(\d+) passed', run.read_log('ui'))
            return PASS, f"npm ci, vitest ({tests[-1] if tests else '?'} passed), build"

        def e2e():
            # The job object holds Playwright, Edge and the Vite dev server it
            # starts; all are stopped and counted gone when the step ends.
            argv = [npm, 'run', 'test:e2e', '--', f'--workers={run.args.e2e_workers}']
            ok, output = run.execute(run.steps['e2e'], argv, cwd=ui_dir, env=env,
                                     timeout=run.args.e2e_timeout)
            found = re.findall(r'(\d+) (passed|failed|flaky|skipped)', output)
            detail = ', '.join(f'{n} {k}' for n, k in found[-4:]) or 'no result line'
            return (PASS if ok else FAIL), detail + f' (Edge channel, {run.args.e2e_workers} worker(s))'

        if run.timed(run.steps['ui'], ui):
            run.timed(run.steps['e2e'], e2e)
        elif not run.stopped:
            run.steps['e2e'].detail = 'skipped because ui failed'

    docker = shutil.which('docker', path=env['PATH'])
    probe = subprocess.run([docker, 'info', '--format', '{{.ServerVersion}}'], capture_output=True,
                           text=True, env=env, timeout=60) if docker and not run.args.skip_docker else None
    if probe is None or probe.returncode != 0:
        reason = ('--skip-docker given' if run.args.skip_docker else
                  'Docker engine unavailable (start Docker Desktop)')
        for name in ('image', 'smokes', 'pidfd-slim'):
            run.steps[name].detail = reason
        return
    run.docker_version = probe.stdout.strip()
    # The smoke scripts accept only the release repository name; this tag is
    # local-only, unique to this run, and never pushed.
    tag = f'ghcr.io/ledgetoptechnologies/engineering-hub-control:build-local-{run.commit[:12]}-{run.nonce}'
    run.image_tag = tag

    def smoke_volumes():
        result = subprocess.run([docker, 'volume', 'ls', '-q', '--filter', f'label={SMOKE_VOLUME_LABEL}'],
                                capture_output=True, text=True, env=env, timeout=60)
        return set(result.stdout.split())

    def after_smoke(before):
        def cleanup(killed):
            # Containers created from this run's unique tag are ours (exact
            # reference match, not just a shared image id).
            leftovers = remove_containers(docker, [f'ancestor={tag}'], env=env, with_volumes=True,
                                          exact_image=tag)
            if killed:
                # A killed check may have created a labelled volume before any
                # container used it. Other agents run the same checks, so such
                # a volume cannot be proven ours: report it, never remove it.
                new = sorted(smoke_volumes() - before)
                leftovers += [f'volume {v} appeared during the killed check; ownership not provable, '
                              f'not removed' for v in new]
            return leftovers
        return cleanup

    def build():
        ok, _ = run.execute(run.steps['image'], [docker, 'build', '-f', 'docker/Dockerfile.hub-control',
                            '--build-arg', f'VCS_REF={run.commit}', '-t', tag, '.'],
                            cwd=run.src, env=env, timeout=1800)
        if not ok:
            return FAIL, 'docker build failed'
        image_id = subprocess.run([docker, 'image', 'inspect', '--format', '{{.Id}}', tag],
                                  capture_output=True, text=True, env=env, timeout=60).stdout.strip()
        return PASS, f'{tag} {image_id[:19]}'

    def smokes():
        passed = []
        for smoke in SMOKES:
            before = smoke_volumes()
            ok, _ = run.execute(run.steps['smokes'], [sys.executable, '-B', f'scripts/{smoke}.py',
                                '--image', tag, '--commit', run.commit], cwd=run.src, env=env,
                                timeout=900, after=after_smoke(before))
            if not ok:
                return FAIL, f'{smoke} failed after {len(passed)} passed'
            passed.append(smoke)
        return PASS, f'{len(passed)}/{len(SMOKES)} isolated container checks'

    def pidfd_slim():
        owner = f'{run.nonce}-pidfd'
        inner = run.args.pidfd_timeout
        ok, output = run.execute(run.steps['pidfd-slim'], [sys.executable, '-B',
                                 'scripts/hub_pidfd_slim_check.py', '--source', str(run.src),
                                 '--owner', owner, '--timeout', str(inner),
                                 '--cpus', run.args.docker_cpus, '--memory', run.args.docker_memory],
                                 cwd=run.src, env=env, timeout=inner + 120,  # grace for the helper's own cleanup
                                 after=lambda killed: remove_containers(docker, [f'label={PIDFD_LABEL}={owner}'],
                                                                        env=env))
        line = [l for l in output.splitlines() if l.startswith('pidfd slim check:') or l.startswith('FAIL')]
        return (PASS if ok else FAIL), (line[-1] if line else 'no result line')[18:].strip()

    if run.timed(run.steps['image'], build):
        run.timed(run.steps['smokes'], smokes)
    elif not run.stopped:
        run.steps['smokes'].detail = 'skipped because image build failed'
    run.timed(run.steps['pidfd-slim'], pidfd_slim)


def remove_image(run):
    tag = getattr(run, 'image_tag', None)
    if not tag or run.args.keep:
        return
    docker = shutil.which('docker')
    subprocess.run([docker, 'image', 'rm', tag], capture_output=True, timeout=120)
    if subprocess.run([docker, 'image', 'inspect', tag], capture_output=True, timeout=60).returncode == 0:
        run.leftovers.append(f'image {tag} (still used by a container?)')


def remove_wsl_dir(run, wsl_dir):
    if run.args.keep or not shutil.which('wsl'):
        return
    try:
        wsl_call(run, ['rm', '-rf', wsl_dir], timeout=120)
    except subprocess.TimeoutExpired:
        run.leftovers.append(f'WSL work directory {wsl_dir} (removal timed out)')


# ---------------------------------------------------------------- self-test
STAND_IN = '''import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])
with open(sys.argv[1], 'w') as handle:
    handle.write(f'{os.getpid()} {child.pid}')
time.sleep(300)
'''


def self_test(args):
    """Exercise timeout and cancel cleanup with stand-in steps; exit 0 if all hold."""
    results = []
    base = Path(tempfile.mkdtemp(prefix='eh-premerge-selftest-', dir=Path.home()))
    stand_in = base / 'stand_in.py'
    stand_in.write_text(STAND_IN, encoding='utf-8')
    docker = shutil.which('docker')

    def scenario(name, body):
        say(f'-> self-test: {name}')
        try:
            ok, evidence = body()
        except Exception as error:
            ok, evidence = False, f'{type(error).__name__}: {error}'
        results.append((name, ok, evidence))
        say(f'   {"PASS" if ok else "FAIL"}: {evidence}')

    def new_run(tag):
        work = base / tag
        work.mkdir()
        run = Run(args, work)
        run.nonce = uuid.uuid4().hex[:8]
        return run, run.step('first', 'stand-in step'), run.step('later', 'later step'), work

    def process_case(trigger):
        run, first, later, work = new_run(trigger)
        pids = work / 'pids'
        marker = work / 'later-ran'
        timeout = 4 if trigger == 'timeout' else 120
        if trigger == 'cancel':
            interrupt_main_after(4)
        try:
            run.timed(first, lambda: run.execute(first, [sys.executable, str(stand_in), str(pids)],
                                                 timeout=timeout) and (PASS, 'finished'))
            run.timed(later, lambda: (marker.write_text('x'), (PASS, 'ran'))[1])
        except StopRun:
            pass
        except KeyboardInterrupt:
            return False, 'Ctrl+C escaped the owned runner'
        owned = [int(p) for p in pids.read_text().split()] if pids.exists() else []
        alive = [p for p in owned if pid_alive(p)]
        ok = (run.stopped is not None and run.stopped.kind == ('timeout' if trigger == 'timeout' else 'cancelled')
              and len(owned) == 2 and not alive and not marker.exists() and later.result == NOT_RUN
              and not run.leftovers)
        return ok, (f'stopped={run.stopped.kind if run.stopped else None}; owned pids {owned} alive after: '
                    f'{alive or "none"}; later step: {later.result}, ran={marker.exists()}')

    scenario('process tree, step timeout', lambda: process_case('timeout'))
    scenario('process tree, Ctrl+C', lambda: process_case('cancel'))

    def wsl_case():
        if not shutil.which('wsl'):
            return False, 'wsl.exe not available'
        run, first, later, work = new_run('wsl')
        token = f'eh-selftest-{run.nonce}'
        script = f"bash -c 'exec -a {token} sleep 300' &\nexec -a {token} sleep 300\n"
        try:
            run.timed(first, lambda: wsl(run, first, script, timeout=6) and (PASS, 'finished'))
            run.timed(later, lambda: (PASS, 'ran'))
        except StopRun:
            pass
        found = wsl_call(run, ['pgrep', '-f', token], timeout=60).stdout.split()
        ok = (run.stopped is not None and run.stopped.kind == 'timeout' and not found
              and later.result == NOT_RUN and not run.leftovers)
        return ok, (f'stopped={run.stopped.kind if run.stopped else None}; processes named {token} left: '
                    f'{found or "none"}; later step: {later.result}')

    scenario('WSL session, step timeout', wsl_case)

    def docker_case(outer):
        if not docker or subprocess.run([docker, 'info'], capture_output=True).returncode:
            return False, 'Docker engine unavailable'
        run, first, later, work = new_run('docker-' + outer)
        owner = f'{run.nonce}-selftest'
        label = f'label={PIDFD_LABEL}={owner}'
        seen = []
        # outer: our runner kills the helper before its own timeout (its
        # finally never runs), so only the run's label-based cleanup can save
        # the day. inner: the helper's own timeout and cleanup.
        inner_timeout, outer_timeout = (600, 25) if outer == 'outer' else (12, 120)
        # A minimal source under the home directory (always mountable by Docker Desktop).
        source = work / 'src'
        (source / 'tests').mkdir(parents=True)
        shutil.copy(REPO / 'tests' / f'{PIDFD_MODULE}.py', source / 'tests')
        argv = [sys.executable, '-B', str(REPO / 'scripts' / 'hub_pidfd_slim_check.py'), '--source', str(source),
                '--owner', owner, '--timeout', str(inner_timeout), '--stand-in-sleep', '300']

        def cleanup(killed):
            seen.append(len(owned_containers(docker, [label])))
            return remove_containers(docker, [label])
        try:
            run.timed(first, lambda: (lambda r: (PASS if r[0] else FAIL, 'helper exit'))(
                run.execute(first, argv, timeout=outer_timeout, after=cleanup)))
            run.timed(later, lambda: (PASS, 'ran'))
        except StopRun:
            pass
        left = owned_containers(docker, [label])
        log = run.read_log('first')
        if outer == 'outer':
            ok = (run.stopped is not None and run.stopped.kind == 'timeout' and seen and seen[0] >= 1
                  and not left and later.result == NOT_RUN)
            evidence = (f'helper killed by outer timeout; owned containers present before run cleanup: '
                        f'{seen[0] if seen else "?"}; after: {len(left)}; later step: {later.result}')
        else:
            ok = (first.result == FAIL and 'removed and verified gone' in log and not left
                  and run.stopped is None)
            evidence = (f'helper timed out itself and reported '
                        f'"{"removed and verified gone" if "removed and verified gone" in log else "?"}"; '
                        f'owned containers after: {len(left)}')
        return ok, evidence

    scenario('pidfd container, helper timeout (inner)', lambda: docker_case('inner'))
    scenario('pidfd container, helper killed (outer timeout)', lambda: docker_case('outer'))

    say('\n### Cleanup self-test (`scripts/local_premerge.py --self-test`)\n')
    say('| scenario | result | evidence |\n|---|---|---|')
    for name, ok, evidence in results:
        say(f'| {name} | {"PASS" if ok else "FAIL"} | {evidence} |')
    if all(ok for _, ok, _ in results):
        shutil.rmtree(base, ignore_errors=True)
        return 0
    say(f'\nSelf-test logs kept in {base}')
    return 1


# ---------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--ref', default='HEAD', help='commit to test (default HEAD)')
    parser.add_argument('--wsl-distro', default='Ubuntu-24.04')
    parser.add_argument('--wsl-venv', default='/root/.venvs/eh-test')
    parser.add_argument('--e2e-workers', type=int, default=1, help='Playwright workers (default 1, as CI)')
    parser.add_argument('--docker-cpus', default='2', help='CPU limit for the pidfd container (default 2)')
    parser.add_argument('--docker-memory', default='4g', help='memory limit for the pidfd container (default 4g)')
    parser.add_argument('--backend-timeout', type=float, default=3600)
    parser.add_argument('--e2e-timeout', type=float, default=1800)
    parser.add_argument('--pidfd-timeout', type=float, default=900,
                        help="the helper's own limit; the run allows it 120 s more to clean up")
    parser.add_argument('--skip-docker', action='store_true', help='report image steps as NOT RUN')
    parser.add_argument('--keep', action='store_true', help='keep work dirs and the built image')
    parser.add_argument('--work-dir', default=None)
    parser.add_argument('--self-test', action='store_true',
                        help='test timeout and Ctrl+C cleanup with stand-in steps, then exit')
    args = parser.parse_args()
    if not WINDOWS:
        raise SystemExit('Run from Windows (Git Bash); it drives WSL for the Linux parts.')
    # Logs carry non-cp1252 characters; never let printing them crash the run.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding='utf-8', errors='replace')
    if args.self_test:
        sys.exit(self_test(args))

    commit = git('rev-parse', '--verify', args.ref + '^{commit}')
    subject = git('log', '-1', '--format=%s', commit)
    dirty = bool(git('status', '--porcelain', '--untracked-files=no')) if args.ref == 'HEAD' else False
    nonce = uuid.uuid4().hex[:8]
    # Not under AppData: Microsoft Store Python silently redirects writes there,
    # where git, Docker and WSL cannot see them.
    work = Path(args.work_dir or Path.home() / '.eh-premerge' / f'{commit[:12]}-{nonce}')
    work.mkdir(parents=True, exist_ok=True)

    run = Run(args, work)
    run.commit, run.nonce, run.src = commit, nonce, work / 'src'
    run.skip_reasons, run.backend_python, run.docker_version = None, None, None
    for name, label in (('backend', 'backend pytest (WSL)'), ('contract', 'UI/DTO contract check'),
                        ('ui', 'hub-ui npm ci, test, build'), ('e2e', 'Playwright e2e'),
                        ('image', 'control image build'), ('smokes', 'isolated container checks'),
                        ('pidfd-slim', 'pidfd tests in python:3.12-slim')):
        run.step(name, label)

    say(f'Local pre-merge run for {commit} ({subject}); logs in {work}')
    tar = work / 'source.tar'
    subprocess.run(['git', '-C', str(REPO), '-c', 'core.autocrlf=false', '-c', 'core.eol=lf', 'archive',
                    '--format=tar', '-o', str(tar), commit], check=True)
    run.src.mkdir()
    with tarfile.open(tar) as archive:
        archive.extractall(run.src, filter='data')
    wsl_dir = f'/root/eh-premerge/{commit[:12]}-{nonce}'

    started = time.monotonic()
    try:
        backend(run, wsl_dir, tar)
        ui_and_docker(run)
    except StopRun:
        pass  # recorded on run.stopped; remaining steps stay NOT RUN
    except KeyboardInterrupt:  # between steps: nothing owned is running
        run.stopped = StopRun('cancelled (Ctrl+C) between steps', 'cancelled')
    finally:
        try:
            remove_image(run)
            remove_wsl_dir(run, wsl_dir)
        except Exception as error:  # still print the summary
            run.leftovers.append(f'final cleanup error: {error}')
    wall = time.monotonic() - started

    for s in run.steps.values():
        if run.stopped and s.result == NOT_RUN and s.detail in ('not started', 'not started: run stopped'):
            s.detail = f'not started: run stopped ({run.stopped.kind})'
    results = [s.result for s in run.steps.values()]
    if run.leftovers:
        verdict = 'CLEANUP NOT PROVEN'
    elif run.stopped and run.stopped.kind == 'cancelled':
        verdict = 'CANCELLED'
    else:
        verdict = FAIL if FAIL in results else ('INCOMPLETE' if NOT_RUN in results else PASS)
    node = subprocess.run(['node', '--version'], capture_output=True, text=True).stdout.strip() \
        if shutil.which('node') else 'none'
    py = run.backend_python
    lines = ['### Local pre-merge run (`scripts/local_premerge.py`)', '',
             f'- commit: `{commit}` {subject}',
             f"- tree: {'DIRTY - uncommitted changes were NOT tested' if dirty else 'clean (tested the committed snapshot)'}",
             f'- host: Windows {platform.version()}; WSL {args.wsl_distro} Python {py[0] if py else "?"}'
             f' (uid {py[2] if py else "?"}, os.pidfd_open={py[1] if py else "?"}); node {node};'
             f' Docker {run.docker_version or "unavailable"}',
             f'- wall time: {fmt(wall)} (steps one at a time; owned processes and containers verified gone after each)',
             '', '| step | result | time | detail |', '|---|---|---|---|']
    for s in run.steps.values():
        lines.append(f'| {s.label} | {s.result} | {fmt(s.seconds)} | {s.detail} |')
    if run.skip_reasons:
        lines += ['', f'Backend skips ({sum(run.skip_reasons.values())}), top reasons:']
        for reason, count in run.skip_reasons.most_common(6):
            lines.append(f'- {count} x {reason}')
    not_run = [s.label for s in run.steps.values() if s.result == NOT_RUN]
    lines += ['', f"Not run: {', '.join(not_run) if not_run else 'none'}",
              'Not covered locally: GHCR push, registry-digest re-test and `latest` promotion (publish job only).']
    if run.leftovers:
        lines += ['', '**Left behind (cleanup not proven; remove by hand):**']
        lines += [f'- {item}' for item in run.leftovers]
    lines.append(f'**Verdict: {verdict}**')
    summary = '\n'.join(lines)
    (work / 'summary.md').write_text(summary + '\n', encoding='utf-8')
    print('\n' + summary)
    if run.leftovers:
        sys.exit(3)
    if verdict == 'CANCELLED':
        sys.exit(130)
    sys.exit(0 if verdict == PASS else 1 if verdict == FAIL else 2)


if __name__ == '__main__':
    main()
