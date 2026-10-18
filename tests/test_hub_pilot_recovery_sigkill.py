"""Real Linux controller SIGKILL recovery with SIMULATED host receipts.

This proves process/SQLite continuity, not Docker, real app or reboot acceptance.
"""
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import selectors
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

import pytest

from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.pilot_ownership import PilotOwner, PilotOwnershipError
from engineering_hub.pilot_recovery import PilotRecovery
from engineering_hub.repository import HubRepository
from tests.test_hub_container_pilot import PRINCIPAL, FakeClient, pilot_manifest


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux process death and flock')


CHILD_SOURCE = '''
import asyncio, json, signal, sys
from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.repository import HubRepository
from tests.test_hub_container_pilot import PRINCIPAL, IMAGE, FakeClient, pilot_manifest, reviewed_task

with PilotOwner(sys.argv[1], allow_ephemeral_test_storage=True) as owner:
    repo = HubRepository(sys.argv[1], execution_principal=PRINCIPAL, pilot_owner=owner)
    class InterruptedClient(FakeClient):
        async def submit_command(self, command):
            print(json.dumps(dict(epoch=owner.epoch, command=command.model_dump())), flush=True)
            signal.pause()
            raise AssertionError('The parent must kill this controller before any response')
    target = ContainerPilotTarget.from_manifest(pilot_manifest())
    workflow = ContainerPilotWorkflow(repo, InterruptedClient(), [target], pilot_owner=owner)
    task = reviewed_task(repo)
    run = workflow.create_run('codex', task['id'], task['revision'], IMAGE, target.target_id)
    asyncio.run(workflow.advance(run['id']))
'''


def readiness(child):
    """Bound actual pipe readiness and bytes, never an indefinite readline."""
    deadline = time.monotonic() + 10
    output = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ)
        while b'\n' not in output:
            remaining = deadline - time.monotonic()
            assert remaining > 0, 'Controller reservation readiness timed out'
            assert selector.select(remaining), 'Controller reservation readiness timed out'
            chunk = os.read(child.stdout.fileno(), 4096)
            assert chunk, 'Controller exited before reservation readiness'
            output.extend(chunk)
            assert len(output) <= 8192, 'Controller readiness exceeded bounded message size'
    line, remainder = output.split(b'\n', 1)
    assert not remainder
    return json.loads(line)


@pytest.mark.asyncio
async def test_sigkill_adopts_ancestor_receipt_get_only_then_posts_only_unreserved_smoke(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    database = private / 'hub.sqlite'
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join((str(root / 'src'), str(root)))
    child = subprocess.Popen([sys.executable, '-u', '-c', CHILD_SOURCE, str(database)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env, cwd=root,
                             close_fds=True, bufsize=0)
    try:
        ready = readiness(child)
        original = ready['command']
        assert original['action'] == 'deploy_container'
        run_id, old_epoch = original['run_id'], ready['epoch']
        assert original['command_id'] == str(uuid.uuid5(uuid.UUID(run_id), 'container_pilot_v1:deploy_container'))
        with closing(sqlite3.connect(database)) as db:
            row = db.execute('SELECT action,payload,state,response_json FROM hub_container_pilot_commands').fetchone()
            assert row[0] == 'deploy_container'
            assert json.loads(row[1]) == original
            assert row[2:] == ('unknown', None)
            advance = db.execute('SELECT run_id,owner FROM hub_container_pilot_advances').fetchone()
            assert advance[0] == run_id
            assert db.execute('SELECT * FROM hub_container_pilot_advance_epochs').fetchone() == (run_id, advance[1], old_epoch)
        with pytest.raises(PilotOwnershipError):
            PilotOwner(database, allow_ephemeral_test_storage=True)
        assert child.poll() is None
        child.send_signal(signal.SIGKILL)
        assert child.wait(timeout=10) == -signal.SIGKILL

        with PilotOwner(database, allow_ephemeral_test_storage=True) as successor:
            assert successor.epoch != old_epoch
            assert successor.previous_epoch == old_epoch
            successor.assert_prior_epoch(old_epoch)
            repo = HubRepository(database, execution_principal=PRINCIPAL, pilot_owner=successor)
            client = FakeClient()  # Typed simulated trusted receipt; no Docker.
            target = ContainerPilotTarget.from_manifest(pilot_manifest())
            workflow = ContainerPilotWorkflow(repo, client, [target], pilot_owner=successor)
            result = await PilotRecovery(workflow).recover(run_id)
            assert result['recovery'] == 'recovered'
            assert result['run']['status'] == 'running'
            assert [command.model_dump() for command in client.inspections] == [original]
            assert client.submissions == []
            with repo._connection() as db:
                assert db.execute('SELECT * FROM hub_container_pilot_advances').fetchall() == []
                assert db.execute('SELECT * FROM hub_container_pilot_advance_epochs').fetchall() == []
                saved = db.execute('SELECT payload,state FROM hub_container_pilot_commands').fetchone()
                assert json.loads(saved['payload']) == original
                assert saved['state'] == 'succeeded'
            completed = await workflow.advance(run_id)
            assert completed['status'] == 'succeeded'
            assert completed['fencing_generation'] == original['fencing_generation']
            assert completed['execution_manifest_digest'] == original['manifest_digest']
            assert [command.action for command in client.submissions] == ['smoke_container']
            assert client.submissions[0].command_id == str(uuid.uuid5(uuid.UUID(run_id), 'container_pilot_v1:smoke_container'))
            assert client.submissions[0].run_id == run_id
            assert repo.get_task(completed['task_id'])['repair_count'] == 0
            assert await workflow.advance(run_id) == completed
            assert len(client.inspections) == len(client.submissions) == 1
    finally:
        try:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
        finally:
            with ExitStack() as stack:
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None:
                        stack.callback(stream.close)
