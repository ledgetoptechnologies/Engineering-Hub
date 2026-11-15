import pytest

from engineering_hub.repository import HubError
from engineering_hub.work_queue import WorkWorker
from tests.test_hub_staging_controller import env, payload


@pytest.mark.asyncio
@pytest.mark.parametrize('binding', ['absent', 'mismatched_digest', None, {}, {'scheme': 'git-sha1-tree-v1'}])
@pytest.mark.parametrize('phase', ['accepted', 'dispatching', 'held', 'queued'])
async def test_unbound_active_attempt_is_held_without_worker_poll(env, binding, phase):
    controller, client, resources, _, repo = env
    submitted = await controller.submit('codex', payload(repo))
    claim = controller.queue.claim(worker=controller.worker, lease_seconds=600)
    assert claim['id'] == submitted['job']['id']
    item = controller._read(job_id=claim['id'])
    item.update(phase=phase, claim=claim)
    if binding == 'absent':
        item.pop('source_binding')
    elif binding == 'mismatched_digest':
        item['source_binding'] = dict(item['source_binding'], archive_sha256='f' * 64)
    else:
        item['source_binding'] = binding
    controller._save(item)
    calls = []
    async def worker_job(identity):
        calls.append(identity)
        raise AssertionError('unbound attempt must not poll worker')
    client.job = worker_job
    await controller._recover_pending()
    saved = controller._read(job_id=claim['id'])
    assert saved['phase'] == 'held'
    assert saved['hold_reason'] == 'source_binding_unavailable'
    assert saved['manual_recovery_only'] is True
    assert controller.queue.get_job(claim['id'])['state'] == 'held'
    assert {'reason': 'held_campaign', 'ref': claim['id']} in resources.inspect_job(controller.principal, claim['id'])['protected']
    for _ in range(3):
        controller.recheck_backoff.clear()
        await controller._recover_pending()
    manual = await controller.reconcile(claim['id'])
    assert manual['phase'] == 'held'
    assert manual['hold_reason'] == 'source_binding_unavailable'
    assert 'source_commit_verification' not in manual
    assert calls == [] and client.posts == 0
    status = await controller.status('codex')
    assert any(row['id'] == claim['id'] and row['hold_reason'] == 'source_binding_unavailable'
               for row in status['held_jobs'])


@pytest.mark.asyncio
async def test_persisted_no_effect_proof_remains_recoverable_without_binding(env):
    controller, client, _, _, repo = env
    submitted = await controller.submit('codex', payload(repo))
    claim = controller.queue.claim(worker=controller.worker, lease_seconds=600)
    item = controller._read(job_id=claim['id'])
    item.pop('source_binding')
    item.update(phase='verified', claim=claim, dispatch_proof='never_sent', rejection_reason='admission_changed')
    controller._save(item)
    result = await controller.reconcile(submitted['job']['id'])
    assert result['job']['state'] == 'failed'
    assert client.posts == 0


@pytest.mark.asyncio
async def test_historical_terminal_record_is_not_rewritten_or_polled(env):
    controller, client, _, _, repo = env
    submitted = await controller.submit('codex', payload(repo))
    finished = await controller.run_once()
    assert finished['job']['state'] == 'succeeded'
    item = controller._read(job_id=submitted['job']['id'])
    item.pop('source_binding')
    controller._save(item)
    async def forbidden_poll(identity):
        raise AssertionError('historical terminal result must not be polled')
    client.job = forbidden_poll
    result = await controller.reconcile(submitted['job']['id'])
    assert result['job']['state'] == 'succeeded'
    assert result['phase'] == 'verified'
    assert 'source_commit_verification' not in result
    assert controller._read(job_id=submitted['job']['id']) == item


@pytest.mark.asyncio
async def test_unclaimed_legacy_row_visible_as_held_without_fabricated_reservation(env):
    controller, client, _, _, repo = env
    submitted = await controller.submit('codex', payload(repo))
    identity = submitted['job']['id']
    item = controller._read(job_id=identity)
    item.pop('source_binding')
    controller._save(item)
    await controller._recover_pending()
    observed = controller.get_job('codex', identity)
    assert observed['phase'] == 'held'
    assert observed['hold_reason'] == 'source_binding_unavailable'
    assert observed['job']['state'] == 'queued'
    assert controller.queue.claim(worker=controller.worker) is None
    with repo._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM hub_work_machines WHERE job_id=?", (identity,)).fetchone()[0] == 0
    assert any(row['id'] == identity and row['phase'] == 'held'
               for row in (await controller.status('codex'))['held_jobs'])
    assert client.posts == 0


@pytest.mark.asyncio
async def test_late_binding_does_not_implicitly_clear_legacy_hold_or_claim_verification(env):
    controller, client, _, _, repo = env
    submitted = await controller.submit('codex', payload(repo))
    identity = submitted['job']['id']
    item = controller._read(job_id=identity)
    saved_binding = item.pop('source_binding')
    controller._save(item)
    await controller._recover_pending()
    held = controller._read(job_id=identity)
    held['source_binding'] = saved_binding  # Unreviewed out-of-band restoration is not authority.
    held['dispatch_proof'] = 'never_sent'  # Nor is a later unverified no-effect marker.
    held['rejection_reason'] = 'admission_changed'
    controller._save(held)
    await controller._recover_pending()
    observed = await controller.reconcile(identity)
    assert observed['phase'] == 'held'
    assert observed['hold_reason'] == 'source_binding_unavailable'
    assert 'source_binding' not in observed and 'source_commit_verification' not in observed
    assert controller.queue.claim(worker=controller.worker) is None
    assert client.posts == 0


@pytest.mark.asyncio
async def test_interrupted_hold_finishes_before_auto_poll_suppression(env, monkeypatch):
    controller, client, resources, _, repo = env
    submitted = await controller.submit('codex', payload(repo))
    claim = controller.queue.claim(worker=controller.worker, lease_seconds=600)
    item = controller._read(job_id=claim['id'])
    item.pop('source_binding')
    item.update(phase='accepted', claim=claim)
    controller._save(item)
    original = controller._hold
    def interrupted(item, claim=None):
        item['phase'] = 'held'
        controller._save(item)
        raise RuntimeError('interrupted before reservation protection')
    monkeypatch.setattr(controller, '_hold', interrupted)
    await controller._recover_pending()
    partial = controller._read(job_id=claim['id'])
    assert partial['phase'] == 'held'
    assert partial.get('manual_recovery_only') is not True
    monkeypatch.setattr(controller, '_hold', original)
    controller.recheck_backoff.clear()
    await controller._recover_pending()
    fixed = controller._read(job_id=claim['id'])
    assert fixed['manual_recovery_only'] is True
    assert controller.queue.get_job(claim['id'])['state'] == 'held'
    assert {'reason': 'held_campaign', 'ref': claim['id']} in resources.inspect_job(controller.principal, claim['id'])['protected']
    assert client.posts == 0


@pytest.mark.asyncio
async def test_unexpired_other_generation_is_not_mutated_or_polled(env):
    controller, client, _, _, repo = env
    submitted = await controller.submit('codex', payload(repo))
    claim = controller.queue.claim(worker=controller.worker, lease_seconds=600)
    item = controller._read(job_id=claim['id'])
    item.pop('source_binding')
    item.update(phase='accepted', claim=claim)
    controller._save(item)
    import uuid
    other = WorkWorker(controller.worker.agent, str(uuid.uuid4()))
    with repo._transaction() as db:
        db.execute('UPDATE hub_work_jobs SET worker=? WHERE id=?', (other.encoded(), claim['id']))
    async def forbidden_poll(identity):
        raise AssertionError('must not poll another active generation')
    client.job = forbidden_poll
    await controller._recover_pending()
    with pytest.raises(HubError, match='staging_reconciliation_pending'):
        await controller.reconcile(submitted['job']['id'])
    assert controller._read(job_id=claim['id']) == item
    assert controller.queue.get_job(claim['id'])['state'] == 'claimed'
    assert client.posts == 0


@pytest.mark.asyncio
async def test_malformed_legacy_claim_retains_reservation_without_repeated_recovery(env):
    controller, client, resources, _, repo = env
    submitted = await controller.submit('codex', payload(repo))
    claim = controller.queue.claim(worker=controller.worker, lease_seconds=600)
    item = controller._read(job_id=claim['id'])
    item.pop('source_binding')
    item.update(phase='accepted', claim={'unexpected': 1})
    controller._save(item)
    await controller._recover_pending()
    saved = controller._read(job_id=claim['id'])
    assert saved['phase'] == 'held' and saved['manual_recovery_only'] is True
    assert saved['hold_reason'] == 'source_binding_unavailable'
    assert controller.queue.get_job(submitted['job']['id'])['state'] == 'claimed'
    assert {'reason': 'held_campaign', 'ref': claim['id']} in resources.inspect_job(controller.principal, claim['id'])['protected']
    controller.recheck_backoff.clear()
    await controller._recover_pending()
    assert controller._read(job_id=claim['id']) == saved
    assert client.posts == 0
