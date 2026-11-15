import copy
import pytest

from tests.test_hub_staging_controller import env, payload
from tests.test_staging_profile_task_binding import PROFILE, confirmation


def bound_request(env):
    controller, client, resources, _, repo = env
    async def protocol_status():
        return {'schema':'hub-staging.v2', 'profile_protocol':'hub-staging-profile.v2',
                'profile_protocol_ready':True, 'projects':{'engineering-hub':['static']}}
    client.status_v2 = protocol_status
    enrolled = resources.get_profile('human:1', 'engineering-hub', 'static')
    request = payload(repo)
    task = repo.get_task(request['review_task_id'])
    candidate = repo.set_candidate('codex', task['id'], task['revision'], request['source_commit'],
                                   {'recipe':'static', 'generation':1})
    reviewed = repo.review_task('claude', task['id'], candidate['revision'], request['source_commit'],
                                'approve', confirmation(enrolled))
    request['review_task_revision'] = reviewed['revision']
    return request, enrolled


@pytest.mark.asyncio
async def test_profile_bound_hub_queue_dispatch_and_terminal_proof(env, monkeypatch):
    from engineering_hub import staging_controller
    controller, client, _, _, _ = env
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', True)
    request, enrolled = bound_request(env)
    original = client.submit
    async def prove(payload):
        receipt = await original(payload)
        receipt['result']['profile_digest'] = payload['reviewed_profile']['digest']
        return receipt
    monkeypatch.setattr(client, 'submit', prove)
    queued = await controller.submit('codex', request)
    item = controller._read(job_id=queued['job']['id'])
    assert item['reviewed_profile'] == confirmation(enrolled)
    result = await controller.run_once()
    assert result['job']['state'] == 'succeeded'
    assert client.posts == 1


@pytest.mark.asyncio
async def test_missing_digest_holds_original_queue_reservation_without_repost(env, monkeypatch):
    from engineering_hub import staging_controller
    controller, client, _, _, _ = env
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', True)
    request, _ = bound_request(env)
    original = client.submit
    async def missing(payload):
        receipt = await original(payload)
        receipt['result'].pop('profile_digest')
        return receipt
    monkeypatch.setattr(client, 'submit', missing)
    queued = await controller.submit('codex', request)
    result = await controller.run_once()
    assert result['job']['state'] == 'held'
    assert controller.queue.get_job(queued['job']['id'])['state'] == 'held'
    assert client.posts == 1

@pytest.mark.asyncio
async def test_real_worker_wire_profile_binding_and_lost_ack(tmp_path, monkeypatch):
    from engineering_hub import staging_controller
    from engineering_hub.modular_staging_profile import from_worker_recipe
    from tests.test_hub_staging_wire_integration import Wire, request
    from hub_staging_service import core
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    wire = Wire(tmp_path)
    try:
        monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', True)
        enrolled = wire.resources.get_profile('human:1', 'engineering-hub', 'static')
        command = request(wire.repo)
        task = wire.repo.get_task(command['review_task_id'])
        candidate = wire.repo.set_candidate('codex', task['id'], task['revision'], command['source_commit'],
                                          {'recipe':'static', 'generation':1})
        reviewed = wire.repo.review_task('claude', task['id'], candidate['revision'], command['source_commit'],
                                         'approve', confirmation(enrolled))
        command['review_task_revision'] = reviewed['revision']
        queued = await wire.controller.submit('codex', command)
        wire.drop_jobs = True
        held = await wire.controller.run_once()
        assert held['job']['state'] == 'held' and wire.posts() == 1
        wire.service.wait()
        final = await wire.controller.reconcile(queued['job']['id'])
        assert final['job']['state'] == 'succeeded' and wire.posts() == 1
        receipt = wire.service.status(queued['job']['id'])
        assert receipt['result']['profile_digest'] == enrolled['digest']
    finally:
        await wire.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['success_wrong', 'failed_missing', 'failed_wrong', 'failed_correct'])
async def test_terminal_profile_proof_fails_closed_for_success_and_failure(env, monkeypatch, case):
    from engineering_hub import staging_controller
    controller, client, _, _, _ = env
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', True)
    request, _ = bound_request(env)
    if case.startswith('failed'):
        client.fail = True
    original = client.submit
    async def alter(payload):
        receipt = await original(payload)
        if case.endswith('wrong'):
            receipt['result']['profile_digest'] = 'sha256:' + '0'*64
        elif case.endswith('missing'):
            receipt['result'].pop('profile_digest')
        elif case.endswith('correct'):
            receipt['result']['profile_digest'] = payload['reviewed_profile']['digest']
        return receipt
    monkeypatch.setattr(client, 'submit', alter)
    queued = await controller.submit('codex', request)
    result = await controller.run_once()
    assert result['job']['state'] == ('failed' if case == 'failed_correct' else 'held')
    assert controller.queue.get_job(queued['job']['id'])['state'] == result['job']['state']
    assert client.posts == 1

@pytest.mark.asyncio
async def test_synthetic_v1_shape_held_operation_is_not_reposted_or_released(env, monkeypatch):
    import hashlib
    import json
    from engineering_hub import staging_controller
    controller, client, _, _, repo = env
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', True)
    request, _ = bound_request(env)
    queued = await controller.submit('codex', request)
    client.lost_ack = True
    held = await controller.run_once()
    assert held['job']['state'] == 'held' and client.posts == 1
    item = controller._read(job_id=queued['job']['id'])
    item.pop('reviewed_profile')
    # Reconstruct the exact v1 public fingerprint from the old controller's
    # field set. This is a synthetic journal shape, not an old binary replay.
    public = {key: item[key] for key in ('request_id', 'review_task_id',
        'review_task_revision', 'project', 'recipe', 'source_commit', 'source_digest',
        'source_binding', 'topic')}
    fingerprint = hashlib.sha256(json.dumps(public, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    original_job, original_command = item['job_id'], item['command_id']
    client.jobs[original_job]['result'].pop('profile_digest', None)
    with repo._transaction() as db:
        db.execute('UPDATE hub_staging_submissions SET data=?,fingerprint=? WHERE id=?',
                   (json.dumps(item, sort_keys=True, separators=(',', ':')), fingerprint, item['id']))
    result = await controller.reconcile(queued['job']['id'])
    assert result['job']['state'] == 'held' and client.posts == 1
    assert controller.queue.get_job(queued['job']['id'])['state'] == 'held'
    assert controller._read(job_id=original_job)['command_id'] == original_command

@pytest.mark.asyncio
async def test_disabled_gate_blocks_persisted_queued_job_after_controller_restart(env, monkeypatch):
    from engineering_hub import staging_controller
    from engineering_hub.repository import HubError
    controller, client, resources, _, repo = env
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', True)
    request, _ = bound_request(env)
    queued = await controller.submit('codex', request)
    queue, actions = staging_controller.compose_queue(repo, None,
        {'engineering-hub':['static'], 'landscripting':['static']}, 'staging-pc')
    restarted = staging_controller.StagingController(repo, queue, client, resources=resources,
        control_principal=controller.principal, actions=actions, poll_interval=0.001, max_poll_seconds=0.1)
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', False)
    assert await restarted.run_once() is None
    assert restarted.queue.get_job(queued['job']['id'])['state'] == 'queued'
    assert client.posts == 0


@pytest.mark.asyncio
async def test_gate_changes_after_claim_prevent_worker_post_and_retain_reservation(env, monkeypatch):
    from engineering_hub import staging_controller
    controller, client, _, _, _ = env
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', True)
    request, _ = bound_request(env)
    queued = await controller.submit('codex', request)
    original = controller.queue._owned
    def disable_after_claim(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', False)
        return result
    monkeypatch.setattr(controller.queue, '_owned', disable_after_claim)
    result = await controller.run_once()
    assert result['job']['state'] == 'held'
    assert controller.queue.get_job(queued['job']['id'])['state'] == 'held'
    assert client.posts == 0

@pytest.mark.asyncio
@pytest.mark.parametrize('remote', [
    {'schema':'hub-staging.v1', 'projects':{'engineering-hub':['static']}},
    {'schema':'hub-staging.v2', 'profile_protocol':'hub-staging-profile.v2', 'profile_protocol_ready':False},
    {'schema':'hub-staging.v2', 'profile_protocol':'unknown', 'profile_protocol_ready':True},
])
async def test_mixed_worker_version_denied_before_source_deposit(env, monkeypatch, remote):
    from engineering_hub import staging_controller
    from engineering_hub.repository import HubError
    controller, client, _, _, _ = env
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', True)
    request, _ = bound_request(env)
    async def status():
        return remote
    monkeypatch.setattr(client, 'status_v2', status)
    with pytest.raises(HubError, match='staging_profile_protocol_unavailable'):
        await controller.submit('codex', request)
    assert client.deposits == 0 and client.posts == 0


@pytest.mark.asyncio
async def test_v2_accepted_retry_does_not_require_remote_status(env, monkeypatch):
    from engineering_hub import staging_controller
    from engineering_hub.repository import HubError
    controller, client, _, _, _ = env
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', True)
    request, _ = bound_request(env)
    original = await controller.submit('codex', request)
    async def offline():
        raise AssertionError('accepted retry must not contact worker')
    monkeypatch.setattr(client, 'status_v2', offline)
    again = await controller.submit('codex', request)
    assert again['job']['id'] == original['job']['id'] and client.deposits == 1
    changed = dict(request, topic='different')
    with pytest.raises(HubError, match='staging_request_conflict'):
        await controller.submit('codex', changed)
    assert client.deposits == 1 and client.posts == 0


@pytest.mark.asyncio
async def test_v2_complete_receipt_is_read_only_when_protocol_disabled(env, monkeypatch):
    from engineering_hub import staging_controller
    from engineering_hub.repository import HubError
    controller, client, resources, _, _ = env
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', True)
    request, _ = bound_request(env)
    original = await controller.submit('codex', request)
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', False)
    async def offline():
        raise AssertionError('disabled receipt read must not contact worker')
    monkeypatch.setattr(client, 'status_v2', offline)
    assert (await controller.submit('codex', request))['job']['id'] == original['job']['id']
    with pytest.raises(HubError, match='staging_request_conflict'):
        await controller.submit('codex', dict(request, topic='different'))
    resources.set_policy('human:1', 'engineering-hub', 1, {'codex':['read'], 'claude':['read']})
    with pytest.raises(HubError):
        await controller.submit('codex', request)
    assert client.deposits == 1 and client.posts == 0


@pytest.mark.asyncio
async def test_v2_receipt_read_denies_grant_revoked_during_source_proof(env, monkeypatch):
    from engineering_hub import staging_controller
    from engineering_hub.repository import HubError
    from hub_staging_service import source_provenance
    controller, client, resources, _, _ = env
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', True)
    request, _ = bound_request(env)
    original = await controller.submit('codex', request)
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', False)
    verify = source_provenance.verify
    def revoke_during_proof(*args):
        resources.set_policy('human:1', 'engineering-hub', 1, {'codex':['read'], 'claude':['read']})
        return verify(*args)
    monkeypatch.setattr(source_provenance, 'verify', revoke_during_proof)
    with pytest.raises(HubError):
        await controller.submit('codex', request)
    assert controller.queue.get_job(original['job']['id'])['state'] == 'queued'
    assert client.deposits == 1 and client.posts == 0


@pytest.mark.asyncio
async def test_v2_lost_source_ack_keeps_original_uncertain_without_repost(env, monkeypatch):
    from engineering_hub import staging_controller
    from engineering_hub.repository import HubError
    controller, client, _, _, repo = env
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', True)
    request, _ = bound_request(env)
    original = client.put_source
    async def lost_receipt(payload):
        await original(payload)
        raise TimeoutError('synthetic source acknowledgment loss')
    monkeypatch.setattr(client, 'put_source', lost_receipt)
    with pytest.raises(TimeoutError):
        await controller.submit('codex', request)
    with repo._connection() as db:
        item = __import__('json').loads(db.execute('SELECT data FROM hub_staging_submissions WHERE actor=? AND request_id=?',
            ('codex', request['request_id'])).fetchone()[0])
    assert item['phase'] == 'deposit_unknown' and client.deposits == 1
    with pytest.raises(HubError, match='staging_source_receipt_uncertain'):
        await controller.submit('codex', request)
    assert client.deposits == 1 and client.posts == 0


@pytest.mark.asyncio
async def test_real_source_ack_loss_never_reposts_to_replacement_worker(tmp_path, monkeypatch):
    import json
    from engineering_hub import staging_controller
    from engineering_hub.repository import HubError
    from engineering_hub.staging_client import StagingClientError
    from hub_staging_service import core
    from tests.test_hub_staging_wire_integration import Wire, request
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    original = Wire(tmp_path)
    replacement = None
    original_client = original.controller.client
    try:
        payload = request(original.repo)
        original.drop_sources = True
        with pytest.raises(StagingClientError):
            await original.controller.submit('codex', payload)
        with original.repo._connection() as db:
            row = db.execute('SELECT data FROM hub_staging_submissions WHERE actor=? AND request_id=?',
                             ('codex', payload['request_id'])).fetchone()
        item = json.loads(row['data'])
        assert item['phase'] == 'deposit_unknown' and item['job_id'] is None
        assert original.service.db.execute('SELECT id FROM sources').fetchone()[0] == item['source_id']
        assert original.counts[('POST', '/v2/sources')] == 1 and original.posts() == 0
        other_root = tmp_path / 'replacement'; other_root.mkdir()
        replacement = Wire(other_root)
        original.controller.client = replacement.controller.client
        with pytest.raises(HubError, match='staging_source_receipt_uncertain'):
            await original.controller.submit('codex', payload)
        assert replacement.received == []
        assert original.counts[('POST', '/v2/sources')] == 1 and original.posts() == 0
        assert replacement.service.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 0
        assert original.controller._read(item['id'])['source_id'] == item['source_id']
    finally:
        original.controller.client = original_client
        if replacement is not None:
            await replacement.close()
        await original.close()


@pytest.mark.asyncio
async def test_v2_revocation_before_deposit_intent_denies_remote_effect(env, monkeypatch):
    from engineering_hub import staging_controller
    from engineering_hub.repository import HubError
    controller, client, resources, _, _ = env
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', True)
    request, _ = bound_request(env)
    original = client.status_v2
    async def revoke_before_intent():
        status = await original()
        resources.set_policy('human:1', 'engineering-hub', 1, {'codex':['read'], 'claude':['read']})
        return status
    monkeypatch.setattr(client, 'status_v2', revoke_before_intent)
    with pytest.raises(HubError):
        await controller.submit('codex', request)
    assert client.deposits == 0 and client.posts == 0
