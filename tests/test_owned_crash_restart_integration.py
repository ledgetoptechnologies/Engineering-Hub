"""Owned Linux subprocess dies with a durable held job; restart stays gated.

This is a same-filesystem ownership/recovery test with simulated remote work.
It does not prove independent-host writer fencing or a real Docker effect.
"""
import asyncio
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
from contextlib import suppress

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux flock and SIGSTOP')

from engineering_hub.pilot_ownership import PilotOwner, PilotOwnershipError
from engineering_hub.repository import HubError
from engineering_hub.startup_reconciliation import OperatorReconciliation
from tests.test_hub_queued_pilot import machine_job
from tests.test_shared_pilot_modular_integration import original_hold_rows
from tests.test_shared_staging_lifecycle import lifecycle_case, pending

CHILD = r'''
import asyncio, json, sys
from pathlib import Path
from engineering_hub.unproved_drain import acquire_reservation
from tests.test_shared_pilot_modular_integration import joint_case, original_hold_rows
from tests.test_hub_staging_controller import payload

async def main():
    root = Path(sys.argv[1])
    with joint_case(root) as state:
        state.modular_client.lost_ack = True
        record = await state.modular.submit('codex', payload())
        outcome = await state.modular.run_once()
        assert outcome['job']['state'] == 'held' and state.modular_client.posts == 1
        job = record['job']['id']
        old = original_hold_rows(state, job)
        reservation = acquire_reservation()
        held = asyncio.create_task(reservation.retain_unproved(state.owner, state.modular, state.modular_client, state.policies))
        await asyncio.sleep(0)
        assert reservation.state == 'held' and not held.done()
        print(json.dumps({'epoch': state.owner.epoch, 'job': job, 'rows': old,
                          'posts': state.modular_client.posts}), flush=True)
        await held
asyncio.run(main())
'''

@pytest.mark.asyncio
async def test_crash_restart_same_database_keeps_original_held_work_gated(tmp_path):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parents[1])]))
    child = subprocess.Popen([sys.executable, '-u', '-c', CHILD, str(tmp_path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, close_fds=True)
    try:
        line = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 15)
        assert line, 'child did not enter hold: ' + (child.stderr.read() if child.poll() is not None else '')
        initial = json.loads(line)
        database = tmp_path / 'private' / 'hub.sqlite3'
        os.kill(child.pid, signal.SIGSTOP)
        stopped_pid, status = await asyncio.wait_for(asyncio.to_thread(os.waitpid, child.pid, os.WUNTRACED), 5)
        assert stopped_pid == child.pid and os.WIFSTOPPED(status)
        for _ in range(2):
            with pytest.raises(PilotOwnershipError):
                PilotOwner(database, allow_ephemeral_test_storage=True, require_existing=True)
        with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as db:
            assert db.execute('SELECT epoch FROM hub_pilot_owner WHERE id=1').fetchone()[0] == initial['epoch']
        os.kill(child.pid, signal.SIGKILL)
        await asyncio.wait_for(asyncio.to_thread(child.wait), 5)
        assert child.returncode == -signal.SIGKILL
        async with lifecycle_case(tmp_path, reopening=True, accepted=False) as case:
            assert case.owner.previous_epoch == initial['epoch'] and case.owner.epoch != initial['epoch']
            assert json.loads(json.dumps(original_hold_rows(case, initial['job']))) == initial['rows']
            assert case.queue.get_job(initial['job'])['state'] == 'held'
            assert machine_job(case) == initial['job']
            assert (await pending(case))['state'] == 'pending'
            assert (await case.lifecycle.recover_and_open())['state'] == 'pending'
            assert not case.policies._open and case.runtime._task is None
            with pytest.raises(HubError):
                await case.modular.run_once()
            assert case.modular_client.posts == 0 and case.client.submissions == []
            gate = case.bridge.reconciliation
            gate.accept_operator_reconciliation(owner=case.owner, report=OperatorReconciliation(
                gate.challenge, case.owner.epoch, case.bridge.target.target_id,
                case.bridge.target.manifest_digest, gate._executor, 'sha256:' + 'f' * 64, True, True, True))
            # The fresh fake remote has no receipt for the original job. Human
            # acceptance alone must not invent one or start any new command.
            assert (await case.lifecycle.recover_and_open())['state'] == 'recovery_required'
            assert case.queue.get_job(initial['job'])['state'] == 'held'
            assert machine_job(case) == initial['job']
            assert not case.policies._open and case.runtime._task is None
            assert case.modular_client.posts == 0 and case.client.submissions == []
    finally:
        if child.poll() is None:
            with suppress(ProcessLookupError): os.kill(child.pid, signal.SIGKILL)
            await asyncio.to_thread(child.wait)
        child.stdout.close()
        child.stderr.close()
