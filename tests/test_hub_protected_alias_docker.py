"""Opt-in host Docker acceptance using owned, disposable synthetic volumes.

Never run inside untrusted branch CI or expose a Docker socket to a test job.
No NAS paths, real databases, credentials, network or application are used.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get('EH_DISPOSABLE_DOCKER_ALIAS_TEST') != '1',
    reason='explicit trusted-host disposable Docker test only',
)
LABEL = 'engineering-hub.disposable-alias-test'
PYTHON = '/opt/validation-venv/bin/python'


def docker_command():
    context = os.environ.get('EH_ALIAS_TEST_CONTEXT', '')
    assert context, 'explicitly select a trusted local Docker context'
    return ['docker', '--context', context]


def docker(*args):
    return subprocess.run([*docker_command(), *args], check=True, capture_output=True,
                          text=True, timeout=60).stdout.strip()


@pytest.fixture(scope='module')
def fixture():
    context = os.environ.get('EH_ALIAS_TEST_CONTEXT', '')
    endpoint = docker('context', 'inspect', context, '--format', '{{.Endpoints.docker.Host}}')
    assert endpoint.startswith(('unix:///', 'npipe:////./pipe/')), 'remote Docker endpoint refused'
    image = os.environ.get('EH_ALIAS_TEST_IMAGE', '')
    assert re.fullmatch(r'sha256:[0-9a-f]{64}', image), 'pin an existing reviewed local image'
    assert docker('image', 'inspect', image, '--format', '{{.Id}}') == image
    identity = uuid.uuid4().hex
    volumes, containers = [], []

    def volume(suffix):
        name = f'eh-alias-test-{identity}-{suffix}'
        volumes.append(name)
        docker('volume', 'create', '--label', f'{LABEL}={identity}', name)
        return name

    def run(mounts, script, root=False):
        name = f'eh-alias-test-{identity}-{len(containers)}'
        containers.append(name)
        args = ['run', '--name', name, '--label', f'{LABEL}={identity}', '--rm',
                '--network', 'none', '--read-only', '--cpus', '1', '--memory', '768m',
                '--memory-swap', '768m', '--pids-limit', '128', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges:true', '--user', '0:0' if root else '568:568']
        if root:
            args += ['--cap-add', 'CHOWN']
        for mount in mounts:
            args += ['--mount', mount]
        args += ['--mount', f'type=bind,source={Path(__file__).resolve().parents[1] / "src"},target=/source,readonly',
                 '--env', 'PYTHONPATH=/source', '--env', 'PYTHONDONTWRITEBYTECODE=1',
                 '--entrypoint', PYTHON, image, '-c', script]
        return docker(*args)

    try:
        private, shared = volume('private'), volume('shared')
        run([f'type=volume,source={private},target=/seed',
             f'type=volume,source={shared},target=/shared'], '''
import os
from pathlib import Path
p = Path('/seed/private')
p.mkdir(mode=0o700)
for name, content in [('hub.sqlite3', b'synthetic-not-a-database'), ('hub.sqlite3.pilot-owner.lock', b'')]:
    f = p / name
    f.write_bytes(content)
    f.chmod(0o600)
    os.chown(f, 568, 568)
p.chmod(0o700)
os.chown(p, 568, 568)
Path('/shared').chmod(0o700)
os.chown('/shared', 568, 568)
''', root=True)
        yield private, shared, run
    finally:
        # Only exact UUID-owned objects; never prune or remove host paths.
        for name in containers:
            result = subprocess.run([*docker_command(), 'container', 'inspect', name],
                                    capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                obj = json.loads(result.stdout)[0]
                assert obj['Config']['Labels'].get(LABEL) == identity
                assert obj['Image'] == image
                docker('container', 'rm', '--force', name)
        for name in volumes:
            obj = json.loads(docker('volume', 'inspect', name))[0]
            assert obj['Labels'].get(LABEL) == identity
            docker('volume', 'rm', name)


@pytest.mark.parametrize('case', ['sibling', 'parent-alias', 'external-alias'])
def test_real_volume_mount_alias_admission(fixture, case):
    private, shared, run = fixture
    sub = f'type=volume,source={private},volume-subpath=private'
    mounts = [f'{sub},target=/var/lib/hub-private']
    mounts.append(f'type=volume,source={private if case == "parent-alias" else shared},target=/var/lib/hub')
    if case == 'external-alias':
        mounts.append(f'{sub},target=/outside-private-alias')
    script = '''
from pathlib import Path
from engineering_hub.protected_layout import validate_existing_boundary, ProtectedLayoutError
root = Path('/var/lib/hub-private')
db = root / 'hub.sqlite3'
before = db.read_bytes()
case = CASE
if case != 'sibling':
    alias = Path('/var/lib/hub/private' if case == 'parent-alias' else '/outside-private-alias')
    assert alias.stat().st_dev == root.stat().st_dev
    assert alias.stat().st_ino == root.stat().st_ino
else:
    assert root.stat().st_dev == Path('/var/lib/hub').stat().st_dev
    assert root.stat().st_ino != Path('/var/lib/hub').stat().st_ino
try:
    validate_existing_boundary(db, shared_root=None if case == 'external-alias' else Path('/var/lib/hub'))
except ProtectedLayoutError:
    assert case != 'sibling', 'separate sibling mounts must be accepted'
else:
    assert case == 'sibling', 'real overlapping mount alias was incorrectly admitted'
assert db.read_bytes() == before
assert (root / 'hub.sqlite3.pilot-owner.lock').read_bytes() == b''
print(case + ': PASS')
'''.replace('CASE', repr(case))
    assert run(mounts, script) == f'{case}: PASS'
