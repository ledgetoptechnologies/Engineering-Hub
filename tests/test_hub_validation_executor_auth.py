"""Public REST/MCP tokens never confer the private validation worker identity.

Uses real ASGI/MCP transports and SQLite queue fixtures, not a Linux service,
owner, Docker daemon or execution provider. The private claim is created only
through explicit trusted in-process test composition.
"""
from uuid import uuid4

import pytest

from engineering_hub.work_queue import SOFTWARE_VALIDATION_EXECUTOR, WorkWorker
from tests.test_hub_work_queue_mcp import rig, call, error, rpc


BASE = '/api/hub/v1/work'
AGENTS = ('hermes', 'codex', 'claude')


def enrolled_service_policy(queue, body, instance):
    target = body['target']
    queue.claim_verifier = lambda worker, action, environment, machine, actual: (
        worker.agent == SOFTWARE_VALIDATION_EXECUTOR and worker.instance_id == instance
        and action == body['action_key'] and environment == 'staging' and machine == 'staging-pc'
        and actual.task_id == target['task_id'] and actual.source_commit == target['source_commit']
        and actual.artifact_digest == target['artifact_digest'])


def admit(rig, principal):
    client, _, queue, _, task, payload = rig
    body = payload(task(owner=principal))
    result = call(client, 'hub_work_submit', body, 'test-' + principal)
    assert result['actor'] == principal and result['state'] == 'queued'
    instance = str(uuid4())
    enrolled_service_policy(queue, body, instance)
    return result, instance


@pytest.mark.parametrize('principal', AGENTS)
@pytest.mark.parametrize('transport', ['rest', 'mcp'])
def test_public_agent_cannot_claim_service_only_job_even_with_private_instance(rig, principal, transport):
    client, _, queue, *_ = rig
    job, private_instance = admit(rig, principal)
    # Instance UUID is not authority: even knowledge of the actual enrolled
    # instance plus forged proxy/identity hints cannot select the private actor.
    body = {'instance_id': private_instance, 'action_keys': [job['action_key']]}
    if transport == 'rest':
        response = client.post(BASE + '/claim', headers={
            'Authorization': 'Bearer test-' + principal,
            'X-Agent-Identity': SOFTWARE_VALIDATION_EXECUTOR,
            'X-Hub-Actor': SOFTWARE_VALIDATION_EXECUTOR,
            'X-Worker': SOFTWARE_VALIDATION_EXECUTOR,
        }, json=body)
        assert response.status_code == 200 and response.json()['job'] is None
    else:
        assert call(client, 'hub_work_claim', body, 'test-' + principal)['job'] is None
    assert queue.get_job(job['id'])['state'] == 'queued'
    private = queue.claim(worker=WorkWorker(SOFTWARE_VALIDATION_EXECUTOR, private_instance),
                          action_keys=(job['action_key'],))
    assert private['id'] == job['id'] and private['worker'] == SOFTWARE_VALIDATION_EXECUTOR + ':' + private_instance


@pytest.mark.parametrize('principal', AGENTS)
@pytest.mark.parametrize('transport', ['rest', 'mcp'])
@pytest.mark.parametrize('field', ['actor', 'agent', 'worker'])
def test_public_payload_cannot_choose_private_executor_identity(rig, principal, transport, field):
    client = rig[0]
    job, private_instance = admit(rig, principal)
    body = {'instance_id': private_instance, 'action_keys': [job['action_key']], field: SOFTWARE_VALIDATION_EXECUTOR}
    if transport == 'rest':
        response = client.post(BASE + '/claim', headers={'Authorization': 'Bearer test-' + principal}, json=body)
        assert response.status_code == 422
    else:
        error(call(client, 'hub_work_claim', body, 'test-' + principal), 'invalid_input')
    assert rig[2].get_job(job['id'])['state'] == 'queued'


@pytest.mark.parametrize('principal', AGENTS)
@pytest.mark.parametrize('transport', ['rest', 'mcp'])
def test_public_agent_cannot_renew_or_complete_private_claim_with_known_fence(rig, principal, transport):
    client, _, queue, *_ = rig
    job, private_instance = admit(rig, principal)
    private_worker = WorkWorker(SOFTWARE_VALIDATION_EXECUTOR, private_instance)
    claimed = queue.claim(worker=private_worker, action_keys=(job['action_key'],))
    assert claimed['id'] == job['id']
    proof_checks = []
    def independently_enrolled_proof(result, evidence, outcome):
        proof_checks.append(result['worker'])
        return evidence == 'trusted-private-receipt' and outcome == 'succeeded'
    queue.completion_verifier = independently_enrolled_proof
    body = {'instance_id': private_instance, 'fence': claimed['fence']}
    for operation in ('renew', 'complete'):
        data = dict(body)
        if operation == 'complete':
            data.update(outcome='succeeded', evidence_id='trusted-private-receipt')
        if transport == 'rest':
            response = client.post(BASE + '/jobs/' + job['id'] + '/' + operation,
                                   headers={'Authorization': 'Bearer test-' + principal}, json=data)
            assert response.status_code == 409
            assert response.json()['detail']['code'] == 'work_ownership_lost'
        else:
            error(call(client, 'hub_work_' + operation, {'job_id': job['id'], **data}, 'test-' + principal),
                  'work_ownership_lost')
        current = queue.get_job(job['id'])
        assert current['state'] == 'claimed' and current['worker'] == private_worker.encoded()
        assert current['fence'] == claimed['fence']
    assert proof_checks == []  # Ownership checked before even consuming evidence.
    queue.renew(job_id=job['id'], worker=private_worker, fence=claimed['fence'])
    completed = queue.complete(job_id=job['id'], worker=private_worker, fence=claimed['fence'],
                               outcome='succeeded', evidence_id='trusted-private-receipt')
    assert completed['state'] == 'succeeded'
    assert proof_checks == [private_worker.encoded()]


def test_private_identity_string_is_not_a_public_bearer_token(rig):
    client = rig[0]
    assert client.get(BASE + '/status', headers={'Authorization': 'Bearer ' + SOFTWARE_VALIDATION_EXECUTOR}).status_code == 401
    assert rpc(client, 'tools/list', token=SOFTWARE_VALIDATION_EXECUTOR).status_code == 401
