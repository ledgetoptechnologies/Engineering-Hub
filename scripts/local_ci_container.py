"""Fixed image-owned bootstrap for disposable local development checks."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import time


FILES = ('pyproject.toml', 'uv.lock', 'hub-ui/package.json', 'hub-ui/package-lock.json')
INPUT = Path('/input')
WORK = Path('/tmp/eh-ci-work')
CACHE = Path('/opt/eh-ci')


def extract_snapshot(archive, destination):
    with tarfile.open(archive, 'r:') as source:
        members = []
        size = 0
        for member in source:
            members.append(member)
            size += member.size
            if len(members) > 12000 or size > 128 * 1024**2:
                raise ValueError('Snapshot exceeds local CI bounds')
            path = PurePosixPath(member.name)
            if (not member.isfile() and not member.isdir() or path.is_absolute()
                    or '..' in path.parts or not path.parts or path.parts[0] == '.git'
                    or '\\' in member.name or len(member.name) > 512):
                raise ValueError('Unsafe snapshot member')
        source.extractall(destination, members=members, filter='data')


def run_profile(profile, intent):
    if profile not in {'python', 'ui', 'all'} or len(intent) != 32:
        raise SystemExit('Invalid fixed CI profile')
    # The host verifies the actual kernel cgroup before admitting source tests.
    for _ in range(100):
        gate = INPUT / 'admit.json'
        if gate.exists() and json.loads(gate.read_text()) == {'intent': intent}:
            break
        time.sleep(0.1)
    else:
        raise SystemExit('CI admission unavailable')
    root = WORK
    root.mkdir(mode=0o700)
    extract_snapshot(INPUT / 'source.tar', root)
    expected = json.loads((CACHE / 'dependencies.json').read_text())
    for name in FILES:
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected[name]:
            raise SystemExit('Dependency lock differs; rebuild reviewed CI image first')
    home = root.parent / 'ci-home'
    os.environ.update(PYTHONPATH=str(root / 'src') + ':' + str(root), HOME=str(home),
                      HF_HUB_OFFLINE='1', PYTHONDONTWRITEBYTECODE='1')
    home.mkdir(mode=0o700)
    if profile in {'python', 'all'}:
        subprocess.run(['/opt/validation-venv/bin/python', '-m', 'pytest', '-q',
                        '--basetemp', '/tmp/pytest', '-o', 'cache_dir=/tmp/pytest-cache'], cwd=root, check=True)
    if profile in {'ui', 'all'}:
        # This cache belongs to the reviewed image, not the source archive.
        # Preserve npm's relative .bin links so CLI module paths stay correct.
        shutil.copytree(CACHE / 'node_modules', root / 'hub-ui/node_modules', symlinks=True)
        for command in (['npm', 'test'], ['npm', 'run', 'build']):
            subprocess.run(command, cwd=root / 'hub-ui', check=True)


if __name__ == '__main__':
    run_profile(*sys.argv[1:])
