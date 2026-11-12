"""Disposable cleanup admission is not quiescence or queue-release proof."""
import json
import pytest

from engineering_hub.container_pilot_manifest import PilotManifest
from engineering_hub.container_pilot_client import ContainerPilotEvidence
from hub_container_executor.ledger import SQLiteLedger
from hub_container_executor.adapter import HostAdapter
from tests.test_hub_container_pilot_manifest import make_manifest_data
from tests.test_hub_container_executor_ledger import COMMIT, MANIFEST, request
from tests.test_hub_container_pilot import IMAGE, PRINCIPAL, FakeClient, pilot_manifest, reviewed_task
from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.repository import HubRepository


def test_opt_in_changes_digest_and_old_manifest_stays_canonical():
    old = PilotManifest.from_bytes(json.dumps(make_manifest_data()).encode())
    assert 'disposable_final_cleanup' not in json.loads(old.canonical_bytes())
    assert PilotManifest.from_bytes(old.canonical_bytes()).digest() == old.digest()
    value = json.loads(old.canonical_bytes())
    value['disposable_final_cleanup'] = True
    opted = PilotManifest.from_bytes(json.dumps(value).encode())
    assert opted.disposable_final_cleanup is True and opted.digest() != old.digest()
    for invalid in (False, None, 1, 'true'):
        value['disposable_final_cleanup'] = invalid
        with pytest.raises(ValueError):
            PilotManifest.from_bytes(json.dumps(value).encode())


def test_opt_in_requires_guarded_candidate_stop_before_adapter_opens(tmp_path):
    value = make_manifest_data(); value['disposable_final_cleanup'] = True
    manifest = PilotManifest.from_bytes(json.dumps(value).encode())
    with pytest.raises(ValueError, match='guarded candidate stop'):
        HostAdapter(tmp_path/'unused.sqlite3', manifest, single_pilot_protocol=True)
    assert not (tmp_path/'unused.sqlite3').exists()


def test_cleanup_keeps_exact_target_reserved_even_after_stop(tmp_path):
    root = tmp_path / 'private'; root.mkdir(mode=0o700)
    ledger = SQLiteLedger(root / 'ledger.sqlite3', manifest_digest=MANIFEST,
                          candidate_commit=COMMIT, policy_revision=1,
                          disposable_final_cleanup=True)
    try:
        deploy = request()
        assert ledger.admit(deploy).accepted
        created = ContainerPilotEvidence(container_id='a'*64, image_config_id='sha256:'+'b'*64,
                                          health='unknown', evidence_verified=True)
        ledger.finish(deploy.command_id, state='succeeded', reason=None, evidence=created, generation_id='candidate-x')
        smoke = request('smoke_container', run_id=deploy.run_id)
        assert ledger.admit(smoke).accepted
        healthy = created.model_copy(update={'health':'healthy'})
        ledger.finish(smoke.command_id, state='succeeded', reason=None, evidence=healthy)
        assert ledger.target_snapshot(deploy.target_id)['state'] == 'cleanup_pending'
        competing = request(fence=2)
        assert not ledger.admit(competing).accepted
        wrong = request('cleanup_rollback', run_id=deploy.run_id)
        assert not ledger.admit(wrong).accepted
        cleanup = request('cleanup_candidate', run_id=deploy.run_id)
        assert ledger.admit(cleanup).accepted
        assert ledger.disposable_cleanup_role(deploy.run_id) == 'candidate'
        ledger.finish(cleanup.command_id, state='succeeded', reason=None, evidence=created)
        assert ledger.target_snapshot(deploy.target_id)['state'] == 'held'
        assert not ledger.admit(request(fence=3)).accepted
    finally:
        ledger.close()


def test_cleanup_rejected_without_opt_in(tmp_path):
    root = tmp_path / 'private'; root.mkdir(mode=0o700)
    ledger = SQLiteLedger(root / 'ledger.sqlite3', manifest_digest=MANIFEST,
                          candidate_commit=COMMIT, policy_revision=1)
    try:
        deploy = request(); assert ledger.admit(deploy).accepted
        ledger.finish(deploy.command_id, state='succeeded', reason=None,
                      evidence=ContainerPilotEvidence(container_id='a'*64, image_config_id='sha256:'+'b'*64,
                                                       health='unknown', evidence_verified=True))
        smoke = request('smoke_container', run_id=deploy.run_id); assert ledger.admit(smoke).accepted
        ledger.finish(smoke.command_id, state='succeeded', reason=None,
                      evidence=ContainerPilotEvidence(container_id='a'*64, image_config_id='sha256:'+'b'*64,
                                                       health='healthy', evidence_verified=True))
        assert not ledger.admit(request('cleanup_candidate', run_id=deploy.run_id)).accepted
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_hub_opted_cleanup_holds_after_exact_ack_and_does_not_repost(tmp_path):
    class Client(FakeClient):
        single_pilot_protocol = True

        def response(self, command):
            result = super().response(command)
            if command.action == 'cleanup_candidate':
                return result.model_copy(update={'evidence':result.evidence.model_copy(update={'health':'unknown'})})
            return result

        async def acknowledge_command(self, command, digest):
            return self.response(command)

    value = json.loads(pilot_manifest().canonical_bytes()); value['disposable_final_cleanup'] = True
    target = ContainerPilotTarget.from_manifest(PilotManifest.from_bytes(json.dumps(value).encode()))
    repo = HubRepository(tmp_path/'hub.sqlite3', execution_principal=PRINCIPAL)
    with pytest.raises(ValueError, match='single_pilot'):
        ContainerPilotWorkflow(repo, FakeClient(), [target])
    client = Client()
    workflow = ContainerPilotWorkflow(repo, client, [target], single_pilot_protocol=True)
    task = reviewed_task(repo)
    run = workflow.create_run('codex', task['id'], task['revision'], IMAGE, target.target_id)
    result = await workflow.advance(run['id'])
    assert result['status'] == 'held'
    assert result['events'][-1]['reason'] == 'disposable_cleanup_requires_quiescence'
    assert [item.action for item in client.submissions] == ['deploy_container', 'smoke_container', 'cleanup_candidate']
    assert await workflow.advance(run['id']) == result
    assert len(client.submissions) == 3


@pytest.mark.asyncio
async def test_hub_rollback_cleanup_uses_rollback_role_and_retains_hold(tmp_path):
    class Client(FakeClient):
        single_pilot_protocol = True

        def response(self, command):
            result = super().response(command)
            if command.action == 'smoke_container':
                return result.model_copy(update={'state':'failed','reason':'health_failed',
                    'evidence':result.evidence.model_copy(update={'health':'unhealthy'})})
            if command.action in {'rollback_container', 'smoke_container_rollback', 'cleanup_rollback'}:
                evidence = result.evidence.model_copy(update={
                    'container_id':'f'*64, 'image_config_id':'sha256:'+'d'*64,
                    'health':'unknown' if command.action != 'smoke_container_rollback' else 'healthy'})
                return result.model_copy(update={'evidence':evidence})
            return result

        async def acknowledge_command(self, command, digest):
            return self.response(command)

    value = json.loads(pilot_manifest().canonical_bytes()); value['disposable_final_cleanup'] = True
    target = ContainerPilotTarget.from_manifest(PilotManifest.from_bytes(json.dumps(value).encode()))
    repo = HubRepository(tmp_path/'hub.sqlite3', execution_principal=PRINCIPAL)
    client = Client(); workflow = ContainerPilotWorkflow(repo, client, [target], single_pilot_protocol=True)
    task = reviewed_task(repo)
    run = workflow.create_run('codex', task['id'], task['revision'], IMAGE, target.target_id)
    result = await workflow.advance(run['id'])
    assert result['status'] == 'held'
    assert result['events'][-1]['reason'] == 'disposable_cleanup_requires_quiescence'
    assert [item.action for item in client.submissions] == [
        'deploy_container', 'smoke_container', 'rollback_container',
        'smoke_container_rollback', 'cleanup_rollback']


@pytest.mark.asyncio
async def test_hub_lost_cleanup_response_holds_and_never_reposts(tmp_path):
    class Client(FakeClient):
        single_pilot_protocol = True

        async def acknowledge_command(self, command, digest):
            return self.response(command)

    value = json.loads(pilot_manifest().canonical_bytes()); value['disposable_final_cleanup'] = True
    target = ContainerPilotTarget.from_manifest(PilotManifest.from_bytes(json.dumps(value).encode()))
    repo = HubRepository(tmp_path/'hub.sqlite3', execution_principal=PRINCIPAL)
    client = Client({'cleanup_candidate': TimeoutError()})
    workflow = ContainerPilotWorkflow(repo, client, [target], single_pilot_protocol=True)
    task = reviewed_task(repo)
    run = workflow.create_run('codex', task['id'], task['revision'], IMAGE, target.target_id)
    result = await workflow.advance(run['id'])
    assert result['status'] == 'held'
    assert result['events'][-1]['reason'] == 'disposable_cleanup_outcome_uncertain'
    assert len(client.submissions) == 3
    assert await workflow.advance(run['id']) == result
    assert len(client.submissions) == 3
