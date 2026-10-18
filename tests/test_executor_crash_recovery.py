"""Real SIGKILL with durable running admission, not an exception/mark_held test."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid

import pytest

pytest.importorskip("fcntl", reason="Fixture executor requires Linux kernel flock; verify executor acceptance on Linux")

from hub_executor.fixtures import (
    FIXTURE_CANDIDATE_COMMIT,
    FIXTURE_CANDIDATE_DIGEST,
    FIXTURE_POLICY_REVISION,
    FIXTURE_TARGET,
)


# Fault injection only blocks at the actual effect boundary. It never writes
# ledger status, invokes mark_held, fakes an HTTP result, or skips the real effect.
BLOCK_AT_EFFECT = """
import os
from pathlib import Path
import time
from hub_executor.core import HubExecutor
from hub_executor.__main__ import main
original = HubExecutor._apply_fixture

def blocked(self, command):
    if os.environ['CRASH_AFTER_EFFECT'] == 'true':
        original(self, command)
    Path(os.environ['CRASH_BARRIER_PATH']).write_text('running-at-effect-boundary')
    while True:
        time.sleep(0.05)

HubExecutor._apply_fixture = blocked
main()
"""


def _free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def _request(origin, token, path, payload=None, *, method=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        origin + path, data=data, method=method,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _wait_for_health(process, origin, token):
    deadline = time.monotonic() + 8
    while True:
        assert process.poll() is None, 'fixture service exited before health'
        try:
            assert _request(origin, token, '/health') == {'ok': True}
            return
        except OSError:
            assert time.monotonic() < deadline, 'fixture service health deadline'
            time.sleep(0.03)


def _ledger_state(path, command_id):
    with closing(sqlite3.connect(path, timeout=1)) as connection:
        row = connection.execute(
            'SELECT status,result FROM commands WHERE command_id=?', (command_id,)
        ).fetchone()
    assert row is not None
    assert json.loads(row[1])['state'] == row[0]
    return row[0]


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux local owner-lock crash acceptance')
@pytest.mark.parametrize('after_effect', [False, True], ids=['before-effect', 'after-effect'])
def test_sigkill_running_command_recovers_held_without_effect_replay(tmp_path, after_effect):
    repository = Path(__file__).resolve().parents[1]
    token = 'fixture-only-crash-recovery-not-a-live-credential'
    token_path = tmp_path / 'token'
    token_path.write_text(token)
    token_path.chmod(0o600)
    data = tmp_path / 'data'
    barrier = tmp_path / 'effect-boundary'
    port = _free_port()
    origin = f'http://127.0.0.1:{port}'
    env = os.environ.copy()
    env.pop('HUB_EXECUTOR_TOKEN', None)
    env.update(
        HUB_EXECUTOR_DATA_DIR=str(data), HUB_EXECUTOR_TOKEN_FILE=str(token_path),
        HUB_EXECUTOR_HOST='127.0.0.1', HUB_EXECUTOR_PORT=str(port),
        PYTHONPATH=str(repository), CRASH_BARRIER_PATH=str(barrier),
        CRASH_AFTER_EFFECT=str(after_effect).lower(),
    )
    command = dict(
        command_id=str(uuid.uuid4()), target_id=FIXTURE_TARGET, action='deploy_fixture',
        artifact_digest=FIXTURE_CANDIDATE_DIGEST, candidate_commit=FIXTURE_CANDIDATE_COMMIT,
        policy_revision=FIXTURE_POLICY_REVISION, fencing_generation=1,
    )
    processes = []
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        first = subprocess.Popen(
            [sys.executable, '-c', BLOCK_AT_EFFECT], cwd=repository, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        processes.append(first)
        _wait_for_health(first, origin, token)
        pending = pool.submit(_request, origin, token, '/v1/commands', command)
        deadline = time.monotonic() + 5
        while not barrier.exists():
            assert first.poll() is None
            assert time.monotonic() < deadline, 'command never reached real effect boundary'
            time.sleep(0.01)
        ledger = data / 'ledger.sqlite3'
        state_path = data / 'targets/3d-viewer-staging/state.json'
        assert _ledger_state(ledger, command['command_id']) == 'running'
        if after_effect:
            before = state_path.read_bytes()
            assert json.loads(before)['effect_count'] == 1
        else:
            before = None
            assert not state_path.exists()

        first.kill()  # SIGKILL cannot unwind Python or call _mark_held.
        assert first.wait(timeout=3) == -signal.SIGKILL
        assert isinstance(pending.exception(timeout=3), OSError)
        # Independently inspect durable admission AFTER death, BEFORE reopening.
        assert _ledger_state(ledger, command['command_id']) == 'running'

        restarted = subprocess.Popen(
            [sys.executable, '-m', 'hub_executor'], cwd=repository, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        processes.append(restarted)
        _wait_for_health(restarted, origin, token)
        route = f"/v1/commands/{command['command_id']}"
        lookup = _request(origin, token, route)
        assert lookup['state'] == 'held'
        assert lookup['reason'] == 'outcome_unresolved'
        assert _ledger_state(ledger, command['command_id']) == 'held'
        for _ in range(3):
            replay = _request(origin, token, '/v1/commands', command)
            assert replay['state'] == 'held' and replay['replayed'] is True
            assert _request(origin, token, route)['state'] == 'held'
            assert (state_path.read_bytes() if state_path.exists() else None) == before

        inspected = _request(origin, token, route + '/reconcile', method='POST')
        if after_effect:
            assert inspected['state'] == 'succeeded'
            assert inspected['observed']['effect_count'] == 1
            assert inspected['observed']['evidence_valid'] is True
        else:
            assert inspected['state'] == 'held' and inspected['observed'] == {}
        replay = _request(origin, token, '/v1/commands', command)
        assert replay['replayed'] is True and replay['state'] == inspected['state']
        assert (state_path.read_bytes() if state_path.exists() else None) == before
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
        pool.shutdown(wait=True)
