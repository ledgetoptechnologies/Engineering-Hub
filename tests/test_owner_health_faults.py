"""Synthetic owner-health faults: no deployed host fencing or historical root cause."""
from contextlib import contextmanager
import selectors
import sqlite3
import subprocess
import sys

import pytest

from tests.test_hub_session_acceptance import BASE, browser, case  # fixtures
from tests.test_hub_queued_pilot import ACTION, COMMIT, IMAGE, machine_job, rig
from tests.test_hub_container_pilot import reviewed_task
from engineering_hub.repository import HubError
from engineering_hub.work_queue import WorkTarget

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='real Linux pilot owner required')


def test_exclusive_owner_database_lock_seals_session_without_acceptance(browser):
    client, login, c = browser
    headers = login()
    assert client.get(BASE, headers=headers).status_code == 200
    with sqlite3.connect(c.owner._path) as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
    script = '''import sqlite3, sys\ndb = sqlite3.connect(sys.argv[1], timeout=0)\ndb.execute("BEGIN EXCLUSIVE")\nprint("held", flush=True)\nsys.stdin.readline()\ndb.rollback()\ndb.close()\n'''
    child = subprocess.Popen([sys.executable, '-c', script, str(c.owner._path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=5), 'lock subprocess did not signal readiness'
        assert child.stdout.readline().strip() == 'held'
        rejected = client.get(BASE, headers=headers)
        assert rejected.status_code == 410
        assert rejected.json()['detail']['code'] == 'session_acceptance_unavailable'
    finally:
        try:
            child.communicate(input='release\n' if child.poll() is None else None, timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=5)
            raise
    assert child.returncode == 0
    assert client.get(BASE, headers=headers).status_code == 410
    assert c.records == []
    assert c.gate.admission_state == 'closed'


def test_injected_owner_sqlite_busy_rejects_queued_submission_before_effect(rig, monkeypatch):
    task = reviewed_task(rig.repo, 'owner-busy')
    with rig.repo._connection() as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    calls = []
    @contextmanager
    def busy():
        calls.append(True)
        raise sqlite3.OperationalError('synthetic database is locked')
        yield
    with monkeypatch.context() as patch:
        patch.setattr(rig.owner, '_connect', busy)
        with pytest.raises(HubError) as rejected:
            rig.queue.submit(actor='codex', request_id='owner-busy', action_key=ACTION,
                environment='staging', target=WorkTarget(task['id'], COMMIT, IMAGE))
        assert rejected.value.code == 'work_target_not_verified'
    assert calls
    with rig.repo._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM hub_work_jobs').fetchone()[0] == 0
    assert machine_job(rig) is None
    assert rig.repo.list_runs() == []
    assert rig.client.submissions == []
    rig.owner.assert_active(rig.repo.path)
