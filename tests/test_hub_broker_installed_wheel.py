"""Check the broker runtime from a built wheel without the repository on its import path."""
import importlib.util
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='executor runtime requires Linux')


def test_installed_broker_runtime_reaches_policy_validation(tmp_path):
    # CI may supply an already-cached setuptools wheel. No package is fetched.
    cached = os.environ.get('HUB_TEST_SETUPTOOLS_WHEEL')
    tools = tmp_path / 'build-tools'
    tools.mkdir()
    if cached:
        assert hashlib.sha256(Path(cached).read_bytes()).hexdigest() == (
            '51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670')
        with zipfile.ZipFile(cached) as archive:
            archive.extractall(tools)
    elif importlib.util.find_spec('setuptools') is None:
        pytest.skip('offline setuptools build tool unavailable')

    context = tmp_path / 'context'
    (context / 'src').mkdir(parents=True)
    shutil.copy(ROOT / 'packaging/hub-executor/pyproject.toml', context / 'pyproject.toml')
    shutil.copy(ROOT / 'packaging/hub-executor/README.md', context / 'README.md')
    shutil.copytree(ROOT / 'hub_container_executor', context / 'src/hub_container_executor',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (context / 'src/scripts').mkdir()
    shutil.copy(ROOT / 'scripts/inspect_hub_image_identity.py', context / 'src/scripts')
    wheels = tmp_path / 'wheels'
    wheels.mkdir()
    build = ("import sys;sys.path.insert(0,sys.argv[1]);"
             "from setuptools.build_meta import build_wheel;build_wheel(sys.argv[2])")
    subprocess.run([sys.executable, '-I', '-B', '-c', build, str(tools), str(wheels)],
                   cwd=context, check=True, timeout=60, stdout=subprocess.DEVNULL)
    wheel, = wheels.glob('*.whl')
    site = tmp_path / 'installed'
    site.mkdir()
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)

    # The executor comes from the wheel; the core comes from the source fixture.
    # The repository root and its local-test scripts are absent from sys.path.
    check = '''import importlib.util,sys
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from hub_container_executor import github_broker_owner as owner
import hub_container_executor.source_tree as tree
assert tree.__file__.startswith(sys.argv[1])
assert importlib.util.find_spec('scripts.hub_local_test_launcher') is None
try:
    owner._runtime('/missing-isolated-fixture/runtime.json')
except FileNotFoundError:
    pass
else:
    raise AssertionError('protected policy validation was not reached')
'''
    subprocess.run([sys.executable, '-I', '-B', '-c', check, str(site), str(ROOT / 'src')],
                   cwd=tmp_path, check=True, timeout=20)
