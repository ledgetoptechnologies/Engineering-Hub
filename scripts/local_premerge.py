"""One-command local pre-merge run for the current commit (Windows + WSL + Docker).

Run from Git Bash in a checkout:  python scripts/local_premerge.py

Every step works on a `git archive` snapshot of the exact commit, never on
uncommitted files. Steps (see docs/local-premerge.md):
  backend   full pytest in WSL (default Ubuntu-24.04, root, venv /root/.venvs/eh-test)
            with skip accounting; FAILS if any real-kernel pidfd launcher test
            (tests/test_hub_container_executor_launch_supervisor.py) is skipped
  contract  scripts/check_hub_ui_contract.py (WSL Python, Windows node via interop)
  ui        hub-ui npm ci, npm test, npm run build (Windows)
  e2e       Playwright end-to-end (Windows, Edge channel per playwright.config.ts)
  image     docker build docker/Dockerfile.hub-control with VCS_REF=<commit>
  smokes    the five isolated container checks hub-control.yml runs on the candidate
  pidfd-slim  the pidfd module inside python:3.12-slim (same script CI runs)

A step that cannot run (no Docker, no WSL) is NOT RUN, never PASS. Exit code:
0 all steps passed, 1 any step failed, 2 nothing failed but something was not run.
The final summary is Markdown so a PR body can quote it verbatim.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import collections
import os
from pathlib import Path, PureWindowsPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET

REPO = Path(__file__).resolve().parents[1]
PIDFD_MODULE = 'test_hub_container_executor_launch_supervisor'
SMOKES = ('hub_release_smoke', 'hub_split_container_smoke', 'hub_unified_container_smoke',
          'hub_settings_container_smoke', 'hub_protected_container_smoke')
PASS, FAIL, NOT_RUN = 'PASS', 'FAIL', 'NOT RUN'
_print_lock = threading.Lock()


def say(message):
    with _print_lock:
        print(message, flush=True)


def fmt(seconds):
    if seconds is None:
        return '-'
    minutes, secs = divmod(int(round(seconds)), 60)
    return f'{minutes}m{secs:02d}s' if minutes else f'{secs}s'


class Step:
    def __init__(self, name, label):
        self.name, self.label = name, label
        self.result, self.seconds, self.detail = NOT_RUN, None, 'not started'


class Run:
    def __init__(self, args):
        self.args = args
        self.steps = collections.OrderedDict()
        self.logs = Path(args.work_dir)

    def step(self, name, label):
        self.steps[name] = Step(name, label)
        return self.steps[name]

    def execute(self, step, argv, *, cwd=None, env=None, timeout=1800):
        """Run argv, log to <work>/<step>.log, return (ok, output)."""
        log = self.logs / f'{step.name}.log'
        with open(log, 'a', encoding='utf-8', errors='replace') as handle:
            handle.write('$ ' + ' '.join(map(str, argv)) + '\n')
            handle.flush()
            try:
                proc = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, timeout=timeout)
                output = proc.stdout.decode('utf-8', errors='replace')
                code = proc.returncode
            except subprocess.TimeoutExpired as error:
                output = (error.stdout or b'').decode('utf-8', errors='replace') + '\nTIMEOUT\n'
                code = -1
            except OSError as error:
                output, code = f'{error}\n', -1
            handle.write(output + f'\n[exit {code}]\n')
        return code == 0, output

    def timed(self, step, func):
        say(f'-> {step.label}')
        start = time.monotonic()
        try:
            result, detail = func()
        except Exception as error:  # report, never crash the summary
            result, detail = FAIL, f'{type(error).__name__}: {error}'
        step.result, step.detail = result, detail
        step.seconds = time.monotonic() - start if result != NOT_RUN else None
        say(f'   {step.label}: {result} ({fmt(step.seconds)}) {detail}')
        if result == FAIL:
            log = self.logs / f'{step.name}.log'
            if log.exists():
                tail = log.read_text(encoding='utf-8', errors='replace').splitlines()[-25:]
                say('   --- last log lines ---\n   ' + '\n   '.join(tail))
        return result == PASS


def port_open(port):
    import socket
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(('127.0.0.1', port)) == 0


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


# ---------------------------------------------------------------- backend
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


def wsl(run, step, script_text, timeout):
    path = run.logs / f'{step.name}.sh'
    path.write_text(script_text, encoding='utf-8', newline='\n')
    env = dict(os.environ, MSYS_NO_PATHCONV='1')
    return run.execute(step, ['wsl', '-d', run.args.wsl_distro, '-u', 'root', '--', 'bash',
                              to_wsl(path)], env=env, timeout=timeout)


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
                  .replace('__VENV__', run.args.wsl_venv).replace('__JUNIT__', to_wsl(junit))
                  )
        ok, output = wsl(run, step, script, timeout=3600)
        if not junit.exists():
            return FAIL, 'pytest produced no JUnit report (setup failed; see log)'
        counts, reasons, pidfd = junit_summary(junit)
        run.skip_reasons = reasons
        python = re.search(r'EH_PY (\S+) pidfd_open=(\w+) uid=(\d+)', output)
        run.backend_python = python.groups() if python else None
        detail = (f"{counts['passed']} passed, {counts['skipped']} skipped, {counts['failed']} failed; "
                  f"pidfd module: {pidfd['passed']} passed, {pidfd['skipped']} skipped")
        run.pidfd_detail = (pidfd, python.group(2) if python else '?')
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
        last = (output.strip().splitlines() or ['no output'])[-1]
        return (PASS if ok else FAIL), last[:120]

    run.timed(contract, check_contract)
    if not run.args.keep:
        cleanup = run.logs / 'cleanup.sh'
        cleanup.write_text(f'rm -rf "{wsl_dir}"\n', encoding='utf-8', newline='\n')
        subprocess.run(['wsl', '-d', run.args.wsl_distro, '-u', 'root', '--', 'bash', to_wsl(cleanup)],
                       env=dict(os.environ, MSYS_NO_PATHCONV='1'), capture_output=True)


# ---------------------------------------------------------------- windows side
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
                ok, output = run.execute(run.steps['ui'], argv, cwd=ui_dir, env=env, timeout=900)
                if not ok:
                    return FAIL, ' '.join(argv[1:]) + ' failed'
            tests = re.findall(r'Tests\s+(?:\x1b\[[0-9;]*m)*\s*(\d+) passed', run.read_log('ui'))
            return PASS, f"npm ci, vitest ({tests[-1] if tests else '?'} passed), build"

        def e2e():
            argv = [npm, 'run', 'test:e2e', '--', f'--workers={run.args.e2e_workers}']
            ok, output = run.execute(run.steps['e2e'], argv, cwd=ui_dir, env=env, timeout=1800)
            found = re.findall(r'(\d+) (passed|failed|flaky|skipped)', output)
            detail = ', '.join(f'{n} {k}' for n, k in found[-4:]) or 'no result line'
            detail += f' (Edge channel, {run.args.e2e_workers} worker(s))'
            if port_open(5174):  # playwright.config.ts webServer port
                return FAIL, detail + '; dev server still listening on 5174 afterwards'
            return (PASS if ok else FAIL), detail

        if run.timed(run.steps['ui'], ui):
            run.timed(run.steps['e2e'], e2e)
        else:
            run.steps['e2e'].detail = 'skipped because ui failed'

    docker = shutil.which('docker', path=env['PATH'])
    probe = subprocess.run([docker, 'info', '--format', '{{.ServerVersion}}'], capture_output=True,
                           text=True, env=env) if docker and not run.args.skip_docker else None
    if probe is None or probe.returncode != 0:
        reason = ('--skip-docker given' if run.args.skip_docker else
                  'Docker engine unavailable (start Docker Desktop)')
        for name in ('image', 'smokes', 'pidfd-slim'):
            run.steps[name].detail = reason
        return
    run.docker_version = probe.stdout.strip()
    # The smoke scripts accept only the release repository name; this tag is
    # local-only and never pushed.
    tag = f'ghcr.io/ledgetoptechnologies/engineering-hub-control:build-local-{run.commit[:12]}-{run.nonce}'

    def build():
        ok, output = run.execute(run.steps['image'], [docker, 'build', '-f', 'docker/Dockerfile.hub-control',
                                 '--build-arg', f'VCS_REF={run.commit}', '-t', tag, '.'],
                                 cwd=run.src, env=env, timeout=1800)
        if not ok:
            return FAIL, 'docker build failed'
        image_id = subprocess.run([docker, 'image', 'inspect', '--format', '{{.Id}}', tag],
                                  capture_output=True, text=True, env=env).stdout.strip()
        return PASS, f'{tag} {image_id[:19]}'

    def smokes():
        passed = []
        for smoke in SMOKES:
            ok, _ = run.execute(run.steps['smokes'], [sys.executable, '-B', f'scripts/{smoke}.py',
                                '--image', tag, '--commit', run.commit], cwd=run.src, env=env,
                                timeout=900)
            if not ok:
                return FAIL, f'{smoke} failed after {len(passed)} passed'
            passed.append(smoke)
        return PASS, f'{len(passed)}/{len(SMOKES)} isolated container checks'

    def pidfd_slim():
        ok, output = run.execute(run.steps['pidfd-slim'], [sys.executable, '-B',
                                 'scripts/hub_pidfd_slim_check.py', '--source', str(run.src),
                                 '--cpus', run.args.docker_cpus, '--memory', run.args.docker_memory],
                                 cwd=run.src, env=env, timeout=900)
        line = [l for l in output.splitlines() if l.startswith('pidfd slim check:') or l.startswith('FAIL')]
        return (PASS if ok else FAIL), (line[-1] if line else 'no result line')[18:].strip()

    if run.timed(run.steps['image'], build):
        run.timed(run.steps['smokes'], smokes)
        if not run.args.keep:
            subprocess.run([docker, 'image', 'rm', tag], capture_output=True, env=env)
    else:
        run.steps['smokes'].detail = 'skipped because image build failed'
    run.timed(run.steps['pidfd-slim'], pidfd_slim)


# ---------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--ref', default='HEAD', help='commit to test (default HEAD)')
    parser.add_argument('--wsl-distro', default='Ubuntu-24.04')
    parser.add_argument('--wsl-venv', default='/root/.venvs/eh-test')
    parser.add_argument('--e2e-workers', type=int, default=1, help='Playwright workers (default 1, as CI)')
    parser.add_argument('--docker-cpus', default='2', help='CPU limit for the pidfd container (default 2)')
    parser.add_argument('--docker-memory', default='4g', help='memory limit for the pidfd container (default 4g)')
    parser.add_argument('--skip-docker', action='store_true', help='report image steps as NOT RUN')
    parser.add_argument('--parallel', action='store_true',
                        help='run the WSL backend concurrently with the Windows/Docker steps (default: one heavy step at a time)')
    parser.add_argument('--keep', action='store_true', help='keep work dirs and the built image')
    parser.add_argument('--work-dir', default=None)
    args = parser.parse_args()
    if os.name != 'nt':
        raise SystemExit('Run from Windows (Git Bash); it drives WSL for the Linux parts.')

    commit = git('rev-parse', '--verify', args.ref + '^{commit}')
    subject = git('log', '-1', '--format=%s', commit)
    dirty = bool(git('status', '--porcelain', '--untracked-files=no')) if args.ref == 'HEAD' else False
    nonce = uuid.uuid4().hex[:8]
    # Not under AppData: Microsoft Store Python silently redirects writes there,
    # where git, Docker and WSL cannot see them.
    base = Path.home() / '.eh-premerge'
    args.work_dir = args.work_dir or str(base / f'{commit[:12]}-{nonce}')
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)

    run = Run(args)
    run.commit, run.nonce, run.src = commit, nonce, work / 'src'
    run.skip_reasons, run.backend_python, run.pidfd_detail, run.docker_version = None, None, None, None
    run.read_log = lambda name: (work / f'{name}.log').read_text(encoding='utf-8', errors='replace') \
        if (work / f'{name}.log').exists() else ''
    for name, label in (('backend', 'backend pytest (WSL)'), ('contract', 'UI/DTO contract check'),
                        ('ui', 'hub-ui npm ci, test, build'), ('e2e', 'Playwright e2e'),
                        ('image', 'control image build'), ('smokes', 'isolated container checks'),
                        ('pidfd-slim', 'pidfd tests in python:3.12-slim')):
        run.step(name, label)

    say(f'Local pre-merge run for {commit} ({subject}); logs in {work}')
    tar = work / 'source.tar'
    subprocess.run(['git', '-C', str(REPO), '-c', 'core.autocrlf=false', '-c', 'core.eol=lf', 'archive', '--format=tar',
                    '-o', str(tar), commit], check=True)
    run.src.mkdir()
    with tarfile.open(tar) as archive:
        archive.extractall(run.src, filter='data')
    wsl_dir = f'/root/eh-premerge/{commit[:12]}-{nonce}'

    started = time.monotonic()
    if not args.parallel:
        backend(run, wsl_dir, tar)
        ui_and_docker(run)
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(backend, run, wsl_dir, tar), pool.submit(ui_and_docker, run)]
            for job in jobs:
                job.result()
    wall = time.monotonic() - started

    results = [s.result for s in run.steps.values()]
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
             f'- wall time: {fmt(wall)} (steps {"concurrent" if args.parallel else "one at a time"})',
             '', '| step | result | time | detail |', '|---|---|---|---|']
    for s in run.steps.values():
        lines.append(f'| {s.label} | {s.result} | {fmt(s.seconds)} | {s.detail} |')
    if run.skip_reasons:
        lines += ['', f'Backend skips ({sum(run.skip_reasons.values())}), top reasons:']
        for reason, count in run.skip_reasons.most_common(6):
            lines.append(f'- {count} x {reason}')
    not_run = [s.label for s in run.steps.values() if s.result == NOT_RUN]
    lines += ['', f"Not run: {', '.join(not_run) if not_run else 'none'}",
              'Not covered locally: GHCR push, registry-digest re-test and `latest` promotion (publish job only).',
              f'**Verdict: {verdict}**']
    summary = '\n'.join(lines)
    (work / 'summary.md').write_text(summary + '\n', encoding='utf-8')
    print('\n' + summary)
    sys.exit(0 if verdict == PASS else 1 if verdict == FAIL else 2)


if __name__ == '__main__':
    main()
