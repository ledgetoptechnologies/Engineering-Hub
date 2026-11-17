"""Run the real-kernel pidfd launcher tests inside python:3.12-slim as root.

The GitHub runner's uv-managed Python lacks os.pidfd_open, so those tests skip
in the host pytest run. The runtime images use python:3.12-slim, which has it.
This check runs only tests/test_hub_container_executor_launch_supervisor.py in a
throwaway container (source mounted read-only, copied inside) and FAILS if the binding is
missing, if any test in the module is skipped, or if none ran. Used by
.github/workflows/hub-control.yml and scripts/local_premerge.py.
Needs Docker and network access to PyPI for the locked test dependencies.
The container has an owned name and label; on timeout, Ctrl+C or any error it
is removed and verified gone, or the check fails naming what is left.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

from owned_cleanup import Cancelled, CleanupError, StepTimeout, remove_containers, run_owned

IMAGE = 'python:3.12-slim'
MODULE = 'tests/test_hub_container_executor_launch_supervisor.py'
MARKER = 'EH_PIDFD_RESULT='
OWNER_LABEL = 'eh.pidfd-check.owner'
INNER = r'''
set -eu
python -m pip install -q --no-cache-dir --root-user-action=ignore uv==0.11.17
mkdir /tmp/eh-src
tar -C /src --exclude=./.git --exclude=./hub-ui/node_modules -cf - . | tar -C /tmp/eh-src -xf -
cd /tmp/eh-src
uv sync --locked --no-install-project --quiet
set +e
/tmp/eh-venv/bin/python -m pytest -q -p no:cacheprovider -rs --basetemp=/tmp/eh-pytest \
  --junitxml=/tmp/eh-pidfd.xml MODULE_PATH
status=$?
set -e
/tmp/eh-venv/bin/python - "$status" <<'PY'
import json, os, sys, xml.etree.ElementTree as ET
cases = list(ET.parse('/tmp/eh-pidfd.xml').iter('testcase'))
skipped = [c.get('name') for c in cases if c.find('skipped') is not None]
failed = [c.get('name') for c in cases if c.find('failure') is not None or c.find('error') is not None]
print('MARKER' + json.dumps({'pytest_status': int(sys.argv[1]), 'tests': len(cases),
      'skipped': skipped, 'failed': failed, 'pidfd_open': hasattr(os, 'pidfd_open'),
      'uid': os.getuid(), 'python': sys.version.split()[0]}))
PY
'''.replace('MODULE_PATH', MODULE).replace('MARKER', MARKER)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default='.', help='Repository checkout to mount read-only')
    parser.add_argument('--image', default=IMAGE)
    parser.add_argument('--cpus', help='optional docker --cpus limit')
    parser.add_argument('--memory', help='optional docker --memory limit')
    parser.add_argument('--owner', default=None,
                        help='owner token for the container name/label (default: random)')
    parser.add_argument('--timeout', type=float, default=900, help='seconds before the container is removed')
    parser.add_argument('--stand-in-sleep', type=int, default=None,
                        help='cleanup self-test only: sleep instead of testing (always reported as FAIL)')
    args = parser.parse_args()
    source = Path(args.source).resolve()
    if not (source / MODULE).is_file():
        raise SystemExit(f'{MODULE} not found under {source}')
    owner = args.owner or uuid.uuid4().hex
    if not re.fullmatch(r'[a-z0-9-]{8,64}', owner):
        raise SystemExit('--owner must be 8-64 lowercase letters, digits or dashes')
    name = 'eh-pidfd-' + owner
    label = f'{OWNER_LABEL}={owner}'
    limits = (['--cpus', args.cpus] if args.cpus else []) + (['--memory', args.memory] if args.memory else [])
    inner = INNER if args.stand_in_sleep is None else f'sleep {int(args.stand_in_sleep)}'
    command = ['docker', 'run', '--rm', '--name', name, '--label', label, '--user', '0:0', *limits,
               '--mount', f'type=bind,source={source},target=/src,readonly',
               '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'UV_PROJECT_ENVIRONMENT=/tmp/eh-venv',
               '-e', 'UV_PYTHON_DOWNLOADS=never', '-e', 'UV_LINK_MODE=copy',
               args.image, 'sh', '-c', inner]
    with tempfile.TemporaryDirectory(prefix='eh-pidfd-') as scratch:
        log = os.path.join(scratch, 'docker.log')
        try:
            code, output = run_owned(command, log_path=log, timeout=args.timeout,
                                     after=lambda killed: remove_containers('docker', [f'label={label}']))
        except CleanupError as error:
            sys.stdout.write(open(log, encoding='utf-8', errors='replace').read())
            raise SystemExit(f'FAIL: {error}; remove it with: docker rm -f {name}') from None
        except (StepTimeout, Cancelled) as error:
            sys.stdout.write(open(log, encoding='utf-8', errors='replace').read())
            raise SystemExit(f'FAIL: {type(error).__name__} ({error}); container {name} removed and verified gone') from None
    sys.stdout.write(output)
    lines = [line for line in output.splitlines() if line.startswith(MARKER)]
    if code or not lines:
        raise SystemExit(f'FAIL: pidfd slim check did not complete (docker exit {code})')
    report = json.loads(lines[-1][len(MARKER):])
    problems = []
    if not report['pidfd_open']:
        problems.append('os.pidfd_open missing in ' + args.image)
    if report['uid'] != 0:
        problems.append('container did not run as root')
    if report['tests'] == 0:
        problems.append('no tests ran')
    if report['skipped']:
        problems.append(f"{len(report['skipped'])} skipped: " + ', '.join(report['skipped'][:5]))
    if report['failed'] or report['pytest_status']:
        problems.append(f"pytest exit {report['pytest_status']}, failed: " + ', '.join(report['failed'][:5]))
    summary = (f"pidfd slim check: {report['tests']} tests, {len(report['skipped'])} skipped, "
               f"{len(report['failed'])} failed, python {report['python']}, uid {report['uid']}, "
               f"pidfd_open={report['pidfd_open']}")
    print(summary)
    if problems:
        raise SystemExit('FAIL: ' + '; '.join(problems))


if __name__ == '__main__':
    main()
