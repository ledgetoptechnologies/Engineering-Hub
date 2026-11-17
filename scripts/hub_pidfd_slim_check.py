"""Run the real-kernel pidfd launcher tests inside python:3.12-slim as root.

The GitHub runner's uv-managed Python lacks os.pidfd_open, so those tests skip
in the host pytest run. The runtime images use python:3.12-slim, which has it.
This check runs only tests/test_hub_container_executor_launch_supervisor.py in a
throwaway container (source mounted read-only, copied inside) and FAILS if the binding is
missing, if any test in the module is skipped, or if none ran. Used by
.github/workflows/hub-control.yml and scripts/local_premerge.py.
Needs Docker and network access to PyPI for the locked test dependencies.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

IMAGE = 'python:3.12-slim'
MODULE = 'tests/test_hub_container_executor_launch_supervisor.py'
MARKER = 'EH_PIDFD_RESULT='
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
    args = parser.parse_args()
    source = Path(args.source).resolve()
    if not (source / MODULE).is_file():
        raise SystemExit(f'{MODULE} not found under {source}')
    command = ['docker', 'run', '--rm', '--user', '0:0',
               '--mount', f'type=bind,source={source},target=/src,readonly',
               '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'UV_PROJECT_ENVIRONMENT=/tmp/eh-venv',
               '-e', 'UV_PYTHON_DOWNLOADS=never', '-e', 'UV_LINK_MODE=copy',
               args.image, 'sh', '-c', INNER]
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=900)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    lines = [line for line in result.stdout.splitlines() if line.startswith(MARKER)]
    if result.returncode or not lines:
        raise SystemExit(f'pidfd slim check did not complete (docker exit {result.returncode})')
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
