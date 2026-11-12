"""Synthetic cgroup files and real task-owned pidfd; not host-fence acceptance."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

pytest.importorskip('fcntl')
from hub_container_executor import cgroup_scope_evidence as module
from hub_container_executor.launch_supervisor import _Process


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    if sys.platform != 'linux' or os.getuid() != 0:
        pytest.skip('root and Linux pidfd required')
    root = tmp_path / 'cgroup'
    root.mkdir(mode=0o700)
    for name in ('cgroup.procs', 'cgroup.threads'):
        (root / name).write_text('')
        (root / name).chmod(0o600)
    scope = root / 'scope'
    scope.mkdir(mode=0o700)
    for name, value in {'cgroup.max.depth': '0\n', 'cgroup.max.descendants': '0\n',
        'cgroup.procs': '', 'cgroup.threads': '', 'cgroup.subtree_control': '',
        'cgroup.events': 'populated 1\nfrozen 0\n'}.items():
        (scope / name).write_text(value)
        (scope / name).chmod(0o600)
    monkeypatch.setattr(module, '_CGROUP_ROOT', root)
    monkeypatch.setattr(module, '_filesystem', lambda fd: None)  # synthetic files only
    monkeypatch.setattr(module, '_mount_root', lambda: None)
    monkeypatch.setattr(module, '_membership', lambda process, expected: None)
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    process = _Process(child.pid)
    (scope / 'cgroup.procs').write_text(str(child.pid) + '\n')
    try:
        yield scope, child, process
    finally:
        process.close()
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)


def test_retained_pidfd_and_kernel_scope_must_both_be_empty(fixture):
    scope, child, process = fixture
    with_scope = module.EnrolledCgroupScope('scope', processes=(process,))
    try:
        with pytest.raises(module.ScopeEvidenceError):
            with_scope.observe_empty()
        (scope / 'cgroup.events').write_text('populated 0\nfrozen 0\n')
        (scope / 'cgroup.procs').write_text('')
        with pytest.raises(module.ScopeEvidenceError):
            with_scope.observe_empty()  # a stale empty file cannot replace the pidfd check
        (scope / 'cgroup.events').write_text('populated 1\nfrozen 0\n')
        child.terminate()
        child.wait(timeout=3)
        with pytest.raises(module.ScopeEvidenceError):
            with_scope.observe_empty()  # process death alone is not scope emptiness
        (scope / 'cgroup.procs').write_text('')
        (scope / 'cgroup.events').write_text('populated 0\nfrozen 0\n')
        evidence = with_scope.observe_empty()
        assert evidence.original_members == ((process.pid, process.start),)
        assert evidence.effect_outcome_proven is False
    finally:
        with_scope.close()


@pytest.mark.parametrize('mutation', ['extra-member', 'delegable', 'ancestor-delegable', 'child-scope', 'bad-events'])
def test_scope_denies_ambiguous_or_delegated_state(fixture, mutation):
    scope, child, process = fixture
    if mutation == 'extra-member':
        (scope / 'cgroup.procs').write_text(str(child.pid) + '\n999999\n')
    elif mutation == 'delegable':
        (scope / 'cgroup.procs').chmod(0o666)
    elif mutation == 'ancestor-delegable':
        (scope.parent / 'cgroup.procs').chmod(0o666)
    elif mutation == 'child-scope':
        (scope / 'child').mkdir()
    if mutation != 'bad-events':
        with pytest.raises(module.ScopeEvidenceError):
            module.EnrolledCgroupScope('scope', processes=(process,))
        return
    enrolled = module.EnrolledCgroupScope('scope', processes=(process,))
    try:
        child.terminate()
        child.wait(timeout=3)
        (scope / 'cgroup.procs').write_text('')
        (scope / 'cgroup.events').write_text('populated 0\npopulated 0\nfrozen 0\n')
        with pytest.raises(module.ScopeEvidenceError): enrolled.observe_empty()
    finally:
        enrolled.close()
