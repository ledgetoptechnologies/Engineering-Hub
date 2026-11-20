"""Bounded checks that the pre-merge runner cleans up what it owns.

POSIX paths only (process sessions); the Windows job-object, WSL-session and
container paths are exercised by `python scripts/local_premerge.py --self-test`.
"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))

import local_premerge as lp  # noqa: E402
from owned_cleanup import pid_alive, remove_containers  # noqa: E402

pytestmark = pytest.mark.skipif(os.name == 'nt', reason='POSIX session cleanup; Windows uses --self-test')

STAND_IN = '''import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])
with open(sys.argv[1], 'w') as handle:
    handle.write(f'{os.getpid()} {child.pid}')
time.sleep(300)
'''


def wait_for(path, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists() and len(path.read_text().split()) == 2:
            return [int(p) for p in path.read_text().split()]
        time.sleep(0.05)
    raise AssertionError('stand-in never reported its pids')


def test_timeout_stops_owned_tree_and_later_steps(tmp_path):
    stand_in = tmp_path / 'stand_in.py'
    stand_in.write_text(STAND_IN)
    pids, marker = tmp_path / 'pids', tmp_path / 'later-ran'
    run = lp.Run(SimpleNamespace(), tmp_path)
    first, later = run.step('first', 'stand-in'), run.step('later', 'later')
    with pytest.raises(lp.StopRun) as stop:
        run.timed(first, lambda: run.execute(first, [sys.executable, str(stand_in), str(pids)],
                                             timeout=2) and (lp.PASS, 'finished'))
        run.timed(later, lambda: (marker.write_text('x'), (lp.PASS, 'ran'))[1])
    assert stop.value.kind == 'timeout'
    owned = wait_for(pids)
    assert not [pid for pid in owned if pid_alive(pid)]
    assert first.result == lp.FAIL and later.result == lp.NOT_RUN and not marker.exists()
    assert not run.leftovers
    assert run.timed(later, lambda: (lp.PASS, 'ran')) is False  # a stopped run starts nothing


def test_ctrl_c_stops_owned_tree(tmp_path):
    stand_in = tmp_path / 'stand_in.py'
    stand_in.write_text(STAND_IN)
    pids = tmp_path / 'pids'
    driver = tmp_path / 'driver.py'
    driver.write_text(textwrap.dedent(f'''
        import sys
        from types import SimpleNamespace
        sys.path.insert(0, {str(SCRIPTS)!r})
        import local_premerge as lp
        run = lp.Run(SimpleNamespace(), {str(tmp_path)!r})
        first, later = run.step('first', 'stand-in'), run.step('later', 'later')
        try:
            run.timed(first, lambda: run.execute(first, [sys.executable, {str(stand_in)!r}, {str(pids)!r}],
                                                 timeout=120) and (lp.PASS, 'finished'))
            run.timed(later, lambda: (lp.PASS, 'ran'))
        except lp.StopRun as stop:
            print('STOPPED', stop.kind, later.result, len(run.leftovers))
    '''))
    proc = subprocess.Popen([sys.executable, str(driver)], stdout=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        owned = wait_for(pids)
        os.kill(proc.pid, signal.SIGINT)
        out, _ = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    assert 'STOPPED cancelled NOT RUN 0' in out
    assert not [pid for pid in owned if pid_alive(pid)]


def test_container_cleanup_requires_an_owned_filter():
    with pytest.raises(ValueError):
        remove_containers('docker', [])
