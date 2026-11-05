"""Human review DTOs expose only journaled pilot evidence."""
import sys

import pytest
import httpx

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.pilot_release_evidence import pilot_release_evidence
from engineering_hub.container_pilot import ContainerPilotTarget
from engineering_hub.repository import HubRepository
from tests.test_hub_container_pilot import IMAGE, PRINCIPAL, pilot_manifest, ready_run

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux PilotOwner fencing')


@pytest.fixture
def repo(tmp_path):
    return HubRepository(tmp_path / 'hub.sqlite3', execution_principal=PRINCIPAL)


@pytest.fixture
def target():
    return ContainerPilotTarget.from_manifest(pilot_manifest())


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
    assert evidence.evidence_basis == 'authenticated_worker_report'
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
