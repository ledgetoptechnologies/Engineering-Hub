"""Human review DTOs expose only journaled pilot evidence."""
import json
import sys
import asyncio
import threading
from dataclasses import asdict

import pytest
import httpx

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.pilot_release_evidence import pilot_release_evidence
from engineering_hub.container_pilot import ContainerPilotTarget
from engineering_hub.container_pilot_manifest import PilotManifest
from engineering_hub.repository import HubError, HubRepository
from tests.test_hub_container_pilot import FakeClient, IMAGE, PRINCIPAL, pilot_manifest, ready_run, reviewed_task
from tests.test_hub_container_pilot_manifest import make_manifest_data

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux PilotOwner fencing')


@pytest.fixture
def repo(tmp_path):
    return HubRepository(tmp_path / 'hub.sqlite3', execution_principal=PRINCIPAL)


@pytest.fixture
def target():
    return ContainerPilotTarget.from_manifest(pilot_manifest())


def next_target():
    data = make_manifest_data()
    data['candidate']['image_config_id'] = 'sha256:' + 'c' * 64
    data['candidate']['source_commit'] = 'c' * 40
    data['rollback']['image_config_id'] = 'sha256:' + 'e' * 64
    data['rollback']['source_commit'] = 'e' * 40
    return ContainerPilotTarget.from_manifest(PilotManifest.from_bytes(json.dumps(data).encode('utf-8')))


@pytest.mark.asyncio
async def test_release_evidence_binds_successful_candidate_and_rollback_records(repo, target):
    workflow, run = ready_run(repo, target)
    result = await workflow.advance(run['id'])

    evidence = pilot_release_evidence(repo, workflow, run['id'])

    assert result['status'] == 'succeeded'
    assert evidence.candidate_deploy.state == 'succeeded'
    assert evidence.candidate_deploy.protocol_verified is True
    assert evidence.candidate_deploy.journal_verified is True
    assert evidence.candidate_health_check.reported_health == 'healthy'
    assert evidence.rollback_deploy.state == 'not_started'
    assert evidence.candidate_cleanup is None and evidence.rollback_cleanup is None
    assert evidence.evidence_basis == 'authenticated_worker_report'
    assert evidence.independently_verified is False


@pytest.mark.asyncio
async def test_ack_pending_remains_distinct_from_completed_ack(repo, target):
    class LostAckClient(FakeClient):
        single_pilot_protocol = True
        async def acknowledge_command(self, command, digest):
            raise TimeoutError('ack response lost')
    from engineering_hub.container_pilot import ContainerPilotWorkflow
    workflow = ContainerPilotWorkflow(repo, LostAckClient(), [target], single_pilot_protocol=True)
    task = reviewed_task(repo)
    run = workflow.create_run('codex', task['id'], task['revision'], IMAGE, target.target_id)
    assert await workflow._command(run, target, 'deploy_container') == 'held'
    evidence = pilot_release_evidence(repo, workflow, run['id'])
    assert evidence.candidate_deploy.state == 'ack_pending'
    assert evidence.candidate_deploy.protocol_verified and evidence.candidate_deploy.journal_verified
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_single_pilot_ack_receipts WHERE run_id=?', (run['id'],)).fetchone()[0] == 0
    assert evidence.independently_verified is False


@pytest.mark.asyncio
async def test_disposable_cleanup_is_visible_while_run_remains_held(repo):
    class Client(FakeClient):
        single_pilot_protocol = True
        def response(self, command):
            result = super().response(command)
            if command.action == 'cleanup_candidate':
                return result.model_copy(update={'evidence': result.evidence.model_copy(update={'health': 'unknown'})})
            return result
        async def acknowledge_command(self, command, digest):
            return self.response(command)
    from engineering_hub.container_pilot import ContainerPilotWorkflow
    value = json.loads(pilot_manifest().canonical_bytes()); value['disposable_final_cleanup'] = True
    target = ContainerPilotTarget.from_manifest(PilotManifest.from_bytes(json.dumps(value).encode()))
    workflow = ContainerPilotWorkflow(repo, Client(), [target], single_pilot_protocol=True)
    task = reviewed_task(repo)
    run = workflow.create_run('codex', task['id'], task['revision'], IMAGE, target.target_id)
    assert (await workflow.advance(run['id']))['status'] == 'held'
    evidence = pilot_release_evidence(repo, workflow, run['id'])
    assert evidence.run_status == 'held'
    assert evidence.candidate_cleanup.state == 'succeeded'
    assert evidence.rollback_cleanup.state == 'not_started'
    assert evidence.independently_verified is False


@pytest.mark.asyncio
async def test_release_evidence_fails_closed_when_saved_worker_receipt_is_corrupt(repo, target):
    workflow, run = ready_run(repo, target)
    await workflow.advance(run['id'])
    with repo._transaction() as db:
        db.execute(
            "UPDATE hub_container_pilot_commands SET response_json=? "
            "WHERE run_id=? AND action='deploy_container'",
            ('{not-json', run['id']),
        )

    evidence = pilot_release_evidence(repo, workflow, run['id'])

    assert evidence.candidate_deploy.state == 'inconsistent'
    assert evidence.candidate_deploy.protocol_verified is False
    assert evidence.candidate_deploy.journal_verified is False
    assert evidence.independently_verified is False


def test_release_evidence_read_does_not_pin_or_mutate_a_run(repo, target):
    workflow, run = ready_run(repo, target)
    with repo._connection() as db:
        before_binding = db.execute(
            'SELECT binding FROM hub_container_pilot_bindings WHERE run_id=?', (run['id'],),
        ).fetchone()
    before_revision = repo.get_run(run['id'])['revision']

    evidence = pilot_release_evidence(repo, workflow, run['id'])

    with repo._connection() as db:
        after_binding = db.execute(
            'SELECT binding FROM hub_container_pilot_bindings WHERE run_id=?', (run['id'],),
        ).fetchone()
    assert evidence.candidate_deploy.state == 'not_started'
    assert before_binding is None and after_binding is None
    assert repo.get_run(run['id'])['revision'] == before_revision


def test_release_evidence_is_one_snapshot_during_worker_commit(repo, target, monkeypatch):
    workflow, run = ready_run(repo, target)
    original = workflow._historical_target
    errors = []
    results = []

    def advance_worker():
        try:
            results.append(asyncio.run(workflow.advance(run['id'])))
        except BaseException as exc:
            errors.append(exc)

    def advance_after_manifest(snapshot_run, *, db=None):
        target_at_snapshot = original(snapshot_run, db=db)
        worker = threading.Thread(target=advance_worker, daemon=True)
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive(), 'evidence read blocked the worker commit'
        assert not errors
        return target_at_snapshot

    monkeypatch.setattr(workflow, '_historical_target', advance_after_manifest)
    evidence = pilot_release_evidence(repo, workflow, run['id'])

    assert results[0]['status'] == 'succeeded'
    assert evidence.run_status == run['status']
    assert evidence.candidate_deploy.state == 'not_started'
    assert evidence.candidate_health_check.state == 'not_started'
    # A later read observes the committed state and receipts together.
    monkeypatch.setattr(workflow, '_historical_target', original)
    current = pilot_release_evidence(repo, workflow, run['id'])
    assert current.run_status == 'succeeded'
    assert current.candidate_deploy.state == 'succeeded'
    assert current.candidate_health_check.state == 'succeeded'


def test_release_evidence_uses_saved_manifest_after_workflow_recomposition(repo, target):
    original_workflow, run = ready_run(repo, target)
    replacement_workflow = type(original_workflow)(repo, FakeClient(), [next_target()])

    evidence = pilot_release_evidence(repo, replacement_workflow, run['id'])

    assert evidence.candidate_image_config_id == target.artifact_digest
    assert evidence.pilot_manifest_digest == target.manifest_digest
    assert evidence.rollback_image_config_id == target.rollback_image_config_id
    assert evidence.candidate_deploy.state == 'not_started'
    with pytest.raises(HubError, match='container_pilot_manifest_mismatch'):
        replacement_workflow._target(run, pin=False)


def test_release_evidence_rejects_saved_manifest_under_wrong_digest_key(repo, target):
    workflow, run = ready_run(repo, target)
    replacement = next_target()
    replacement_binding = json.dumps(asdict(replacement), sort_keys=True, separators=(',', ':'))
    with repo._transaction() as db:
        db.execute('DELETE FROM hub_container_pilot_manifest_views')
        db.execute(
            'INSERT INTO hub_container_pilot_manifest_views(manifest_digest,binding) VALUES(?,?)',
            (run['execution_manifest_digest'], replacement_binding),
        )

    with pytest.raises(HubError, match='container_pilot_manifest_history_invalid'):
        pilot_release_evidence(repo, workflow, run['id'])


@pytest.mark.asyncio
async def test_release_evidence_route_requires_human_session(repo, target):
    workflow, run = ready_run(repo, target)
    await workflow.advance(run['id'])
    settings = Settings(repo.path, AuthSettings('test-hermes', 'test-codex'),
                        AuthConfig('https://hub.invalid', beau_github_id=42))
    app = create_app(settings, repository=repo, container_pilot_workflow=workflow)
    session = app.state.sessions.create_session(42)
    path = f'/api/hub/v1/container-pilot/runs/{run["id"]}/release-evidence'

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://hub.invalid',
                                 cookies={'hub_session': session['cookie_token']}) as client:
        response = await client.get(path)
        assert response.status_code == 200
        evidence = response.json()
        assert evidence['candidate_deploy']['state'] == 'succeeded'
        assert evidence['candidate_health_check']['reported_health'] == 'healthy'
        assert evidence['evidence_basis'] == 'authenticated_worker_report'
        assert evidence['independently_verified'] is False

        agent_response = await client.get(path, headers={'Authorization': 'Bearer test-codex'})
        assert agent_response.status_code == 403
