"""Configured application action keys enforce review inside queue transactions."""
import hashlib
import json

import pytest

from engineering_hub.repository import HubError, HubRepository
from engineering_hub.staging_module import compose_staging
from engineering_hub.work_queue import WorkAction
from tests.test_hub_staging_controller import Client, payload
from tests.test_shared_staging_preparation import config_file


@pytest.fixture
def configured(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    client = Client()
    path = config_file(tmp_path, projects={'engineering-hub': {'recipes': ['static']}})
    config = json.loads(path.read_text()); config['admin_actors'] = ['human:1']
    path.write_text(json.dumps(config))
    module = compose_staging(repo, path=path, machine_id='synthetic-machine', client_factory=lambda _: client)
    controller = module.controller
    assert controller is not None
    controller.resources.set_policy('human:1', 'engineering-hub', 0,
                                     {'codex': ['read', 'build', 'test']})
    yield controller, client, repo
    repo.close()


def drift(repo, request):
    # Model another writer/legacy corruption, bypassing normal active-work fences.
    with repo._transaction() as db:
        item = repo._get(db, request['review_task_id'], 'task')
        item['review']['verdict'] = 'reject'
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(item), item['id']))


@pytest.mark.asyncio
async def test_configured_submission_rechecks_after_precheck(configured, monkeypatch):
    controller, client, repo = configured
    request = payload(repo)
    original = controller.queue._target_verified
    def change_after_precheck(*args):
        result = original(*args)
        repo.set_candidate('codex', request['review_task_id'], request['review_task_revision'], 'f' * 40)
        return result
    monkeypatch.setattr(controller.queue, '_target_verified', change_after_precheck)
    with pytest.raises(HubError, match='work_target_not_verified'):
        await controller.submit('codex', request)
    with repo._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM hub_work_jobs').fetchone()[0] == 0
    assert client.posts == 0


@pytest.mark.asyncio
async def test_configured_claim_rechecks_review_in_transaction(configured):
    controller, client, repo = configured
    request = payload(repo)
    job = await controller.submit('codex', request)
    assert job['job']['action_key'].startswith('staging-service.')
    drift(repo, request)
    assert await controller.run_once() is None
    assert controller.queue.get_job(job['job']['id'])['state'] == 'queued'
    assert client.posts == 0


@pytest.mark.asyncio
async def test_configured_dispatch_rechecks_review_after_live_claim(configured, monkeypatch):
    controller, client, repo = configured
    request = payload(repo)
    job = await controller.submit('codex', request)
    original = controller.queue.live_claim
    def change_after_live_claim(**kwargs):
        result = original(**kwargs)
        with pytest.raises(HubError, match='active_work_job'):
            repo.set_candidate('codex', request['review_task_id'], request['review_task_revision'], 'f' * 40)
        drift(repo, request)  # Simulate corrupted/legacy data, not a supported mutation.
        return result
    monkeypatch.setattr(controller.queue, 'live_claim', change_after_live_claim)
    await controller.run_once()
    assert controller.queue.get_job(job['job']['id'])['state'] == 'failed'
    assert client.posts == 0


@pytest.mark.asyncio
async def test_legacy_modular_queued_job_is_preserved_and_never_claimed(configured):
    controller, client, repo = configured
    request = payload(repo)
    job = await controller.submit('codex', request)
    legacy = 'modular.' + hashlib.sha256(b'engineering-hub\0static').hexdigest()
    with repo._transaction() as db:
        db.execute('UPDATE hub_work_jobs SET action_key=? WHERE id=?', (legacy, job['job']['id']))
    controller.queue.actions[legacy] = WorkAction('synthetic-machine', project='engineering-hub')
    with repo._connection() as db:
        row = db.execute('SELECT * FROM hub_work_jobs WHERE id=?', (job['job']['id'],)).fetchone()
        from engineering_hub.work_queue import WorkTarget
        assert not controller.queue._modular_target_in_transaction(db, row['actor'], legacy,
            row['environment'], WorkTarget(**json.loads(row['target'])))
    assert await controller.run_once() is None
    await controller._recover_pending()
    current = controller.queue.get_job(job['job']['id'])
    assert current['state'] == 'queued' and current['action_key'] == legacy
    deposits = client.deposits
    assert (await controller.submit('codex', request))['job']['id'] == job['job']['id']
    assert client.deposits == deposits and client.posts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [False, True])
async def test_configured_and_legacy_open_jobs_block_run_creation(configured, legacy):
    controller, client, repo = configured
    request = payload(repo)
    job = await controller.submit('codex', request)
    if legacy:
        with repo._transaction() as db:
            db.execute('UPDATE hub_work_jobs SET action_key=? WHERE id=?',
                       ('modular.' + hashlib.sha256(b'engineering-hub\0static').hexdigest(), job['job']['id']))
    with pytest.raises(HubError, match='active_work_job'):
        repo.create_run('codex', request['review_task_id'], request['review_task_revision'],
                        'sha256:' + 'b' * 64, 'engineering-hub:staging')
    assert client.posts == 0


@pytest.mark.asyncio
async def test_active_run_blocks_configured_modular_enqueue(configured):
    controller, client, repo = configured
    request = payload(repo)
    repo.create_run('codex', request['review_task_id'], request['review_task_revision'],
                    'sha256:' + 'b' * 64, 'engineering-hub:staging')
    with pytest.raises(HubError):
        await controller.submit('codex', request)
    with repo._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM hub_work_jobs').fetchone()[0] == 0
    assert client.posts == 0


@pytest.mark.asyncio
async def test_configured_action_success_and_wrong_recipe_key_denied(configured):
    from engineering_hub.staging_action_key import staging_action_key
    from engineering_hub.staging_controller import compose_queue
    from engineering_hub.work_queue import WorkTarget
    controller, client, repo = configured
    request = payload(repo)
    job = await controller.submit('codex', request)
    key = staging_action_key('engineering-hub', 'static')
    assert controller.actions['engineering-hub', 'static'] == key == job['job']['action_key']
    _, standalone = compose_queue(repo, None, {'engineering-hub': ['static']}, 'other-machine')
    assert standalone['engineering-hub', 'static'] == key
    with repo._connection() as db:
        row = db.execute('SELECT * FROM hub_work_jobs WHERE id=?', (job['job']['id'],)).fetchone()
        target = WorkTarget(**json.loads(row['target']))
        assert not controller.queue._modular_target_in_transaction(db, row['actor'],
            staging_action_key('engineering-hub', 'other'), row['environment'], target)
        assert not controller.queue._modular_target_in_transaction(db, row['actor'],
            staging_action_key('other-project', 'static'), row['environment'], target)
    assert (await controller.run_once())['job']['state'] == 'succeeded'
    assert client.posts == 1


@pytest.mark.asyncio
async def test_legacy_held_operation_is_not_polled_replayed_or_released(configured):
    controller, client, repo = configured
    request = payload(repo)
    job = await controller.submit('codex', request)
    client.lost_ack = True
    assert (await controller.run_once())['job']['state'] == 'held'
    with repo._transaction() as db:
        db.execute('UPDATE hub_work_jobs SET action_key=? WHERE id=?',
                   ('modular.' + hashlib.sha256(b'engineering-hub\0static').hexdigest(), job['job']['id']))
    async def forbidden(_):
        pytest.fail('legacy operation polled')
    client.job = forbidden
    with pytest.raises(HubError, match='staging_legacy_action_reconciliation_required'):
        await controller.reconcile(job['job']['id'])
    await controller._recover_pending()
    deposits = client.deposits
    assert (await controller.submit('codex', request))['job']['id'] == job['job']['id']
    assert controller.queue.get_job(job['job']['id'])['state'] == 'held'
    assert client.deposits == deposits and client.posts == 1


@pytest.mark.asyncio
async def test_legacy_terminal_history_remains_readable(configured):
    controller, client, repo = configured
    request = payload(repo)
    job = await controller.submit('codex', request)
    assert (await controller.run_once())['job']['state'] == 'succeeded'
    with repo._transaction() as db:
        db.execute('UPDATE hub_work_jobs SET action_key=? WHERE id=?',
                   ('modular.' + hashlib.sha256(b'engineering-hub\0static').hexdigest(), job['job']['id']))
    assert (await controller.reconcile(job['job']['id']))['job']['state'] == 'succeeded'
    assert client.posts == 1


@pytest.mark.asyncio
async def test_legacy_claimed_even_with_no_effect_evidence_is_retained(configured):
    controller, client, repo = configured
    request = payload(repo)
    job = await controller.submit('codex', request)
    claimed = controller.queue.claim(worker=controller.worker,
        action_keys=tuple(controller.actions.values()), lease_seconds=600)
    assert claimed['id'] == job['job']['id']
    with repo._connection() as db:
        assert db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=?',
                          ('synthetic-machine',)).fetchone()[0] == claimed['id']
    legacy = 'modular.' + hashlib.sha256(b'engineering-hub\0static').hexdigest()
    with repo._transaction() as db:
        db.execute('UPDATE hub_work_jobs SET action_key=? WHERE id=?', (legacy, claimed['id']))
        row = db.execute('SELECT data FROM hub_staging_submissions WHERE id=?',
                         (claimed['target']['submission_id'],)).fetchone()
        item = json.loads(row['data']); item['dispatch_proof'] = 'never_sent'
        item['rejection_reason'] = 'restart_before_dispatch'
        db.execute('UPDATE hub_staging_submissions SET data=? WHERE id=?', (json.dumps(item), item['id']))
    async def forbidden(_):
        pytest.fail('legacy operation polled')
    client.job = forbidden
    with pytest.raises(HubError, match='staging_legacy_action_reconciliation_required'):
        await controller.reconcile(claimed['id'])
    await controller._recover_pending()
    assert controller.queue.get_job(claimed['id'])['state'] == 'claimed'
    with repo._connection() as db:
        assert db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=?',
                          ('synthetic-machine',)).fetchone()[0] == claimed['id']
    assert client.posts == 0
