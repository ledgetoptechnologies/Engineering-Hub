"""Independent host-ledger fence retention; NOT machine-wide restore acceptance.

Fresh disposable Hub histories simulate a regressed allocator. No actual NAS
backup is restored, and host effects/health here are explicitly synthetic.
"""
from uuid import uuid4

import httpx
import pytest

pytest.importorskip('fcntl', reason='Real Linux private ledgers required')

from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.container_pilot_client import ContainerPilotClient, ContainerPilotEvidence, ContainerPilotRequest
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.queued_pilot import QueuedContainerPilot
from engineering_hub.repository import HubRepository
from engineering_hub.work_queue import WorkAction, WorkQueue, WorkTarget
from hub_container_executor.asgi import PilotASGI
from hub_container_executor.ledger import SQLiteLedger
from tests.test_hub_container_pilot import COMMIT, IMAGE, PRINCIPAL, pilot_manifest, reviewed_task


class SyntheticEffectsWithRealHostLedger:
    def __init__(self, ledger):
        self.ledger = ledger
        self.effects = []

    def submit(self, command):
        admitted = self.ledger.admit(command)
        if not admitted.accepted:
            return admitted.response
        self.effects.append(command.command_id)
        return self.ledger.finish(command.command_id, state='succeeded', reason=None,
            evidence=ContainerPilotEvidence(container_id='e' * 64, image_config_id=IMAGE,
                                            health='healthy', evidence_verified=True))

    def get(self, command_id):
        return self.ledger.get(command_id)

    def reconcile(self, command_id):
        pytest.fail('control recovery uses GET, never effect reconciliation POST')


def host_ledger(path):
    return SQLiteLedger(path, manifest_digest=pilot_manifest().digest(),
                        candidate_commit=COMMIT, policy_revision=1)


async def one_fresh_hub(root, host):
    root.mkdir(mode=0o700)
    async with ContainerPilotClient('http://127.0.0.1', 'synthetic-host-only',
        transport=httpx.ASGITransport(app=PilotASGI(host, 'synthetic-host-only'))) as client:
        with PilotOwner(root / 'hub.sqlite3', allow_ephemeral_test_storage=True) as owner:
            repo = HubRepository(root / 'hub.sqlite3', execution_principal=PRINCIPAL, pilot_owner=owner)
            workflow = ContainerPilotWorkflow(repo, client,
                [ContainerPilotTarget.from_manifest(pilot_manifest())], pilot_owner=owner)
            queue = WorkQueue(repo, {'engineering-hub.pilot': WorkAction('staging-pc', project='engineering-hub')})
            bridge = QueuedContainerPilot(queue, workflow,
                action_key='engineering-hub.pilot', machine_id='staging-pc')
            task = reviewed_task(repo)
            job = queue.submit(actor='codex', request_id=str(uuid4()),
                action_key='engineering-hub.pilot', environment='staging',
                target=WorkTarget(task['id'], COMMIT, IMAGE))
            result = await bridge.run_once()
            # A fresh/regressed control history allocates fence 1 again.
            assert result['fence'] == 1
            assert len(repo.list_runs()) == 1
            if result['state'] == 'held':
                assert await bridge.run_once() is None
                with repo._connection() as db:
                    assert db.execute('SELECT job_id FROM hub_work_machines').fetchone()[0] == job['id']
            return result


@pytest.mark.asyncio
async def test_regressed_hub_counter_cannot_reuse_independent_host_fence_after_reopen(tmp_path):
    private = tmp_path / 'host-private'
    private.mkdir(mode=0o700)
    path = private / 'host.sqlite3'
    ledger = host_ledger(path)
    try:
        host = SyntheticEffectsWithRealHostLedger(ledger)
        assert (await one_fresh_hub(tmp_path / 'control-before', host))['state'] == 'succeeded'
        assert len(host.effects) == 2
        original = ledger.target_snapshot()
        assert original['fence'] == 1
    finally:
        ledger.close()
    ledger = host_ledger(path)
    try:
        host = SyntheticEffectsWithRealHostLedger(ledger)
        result = await one_fresh_hub(tmp_path / 'control-regressed', host)
        assert result['state'] == 'held' and host.effects == []
        assert ledger.target_snapshot() == original
    finally:
        ledger.close()


def test_higher_fence_cannot_displace_running_host_command(tmp_path):
    private = tmp_path / 'host-private'
    private.mkdir(mode=0o700)
    ledger = host_ledger(private / 'host.sqlite3')
    try:
        def command(fence):
            return ContainerPilotRequest(schema_version='container_pilot_v1',
                command_id=str(uuid4()), run_id=str(uuid4()), target_id='engineering-hub:staging',
                action='deploy_container', manifest_digest=pilot_manifest().digest(),
                candidate_commit=COMMIT, policy_revision=1, fencing_generation=fence)
        first = command(1)
        assert ledger.admit(first).accepted
        denied = ledger.admit(command(2))
        assert not denied.accepted and denied.response.reason == 'target_busy'
        assert ledger.get(first.command_id).state == 'running'
        assert ledger.target_snapshot()['fence'] == 1
    finally:
        ledger.close()
