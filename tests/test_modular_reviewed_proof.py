import base64
import hashlib
import io
import uuid
import zipfile

import pytest

from hub_staging_service.source_provenance import _archive_tree, _object_id, verify
from engineering_hub.repository import HubError
from engineering_hub.work_queue import WorkAction, WorkTarget
from tests.test_hub_staging_controller import env, Client


def request_for(repo):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        info = zipfile.ZipInfo('index.html')
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        archive.writestr(info, b'<h1>fixture</h1>')
    raw = output.getvalue()
    tree = _archive_tree(raw)[0]
    obj = (f'tree {tree}\nauthor Test <test@example.invalid> 1 +0000\n'
           f'committer Test <test@example.invalid> 1 +0000\n\nfixture\n').encode()
    commit = _object_id(b'commit', obj)
    task = repo.create_task('human:1', 'engineering-hub', 'fixture', str(uuid.uuid4()))
    task = repo.claim_task('codex', task['id'], task['revision'])
    task = repo.set_candidate('codex', task['id'], task['revision'], commit)
    task = repo.review_task('claude', task['id'], task['revision'], commit, 'approve')
    return {'request_id': str(uuid.uuid4()), 'review_task_id': task['id'],
            'review_task_revision': task['revision'], 'project': 'engineering-hub', 'recipe': 'static',
            'source_commit': commit, 'source_digest': hashlib.sha256(raw).hexdigest(),
            'source_zip_base64': base64.b64encode(raw).decode(),
            'source_commit_object_base64': base64.b64encode(obj).decode()}


@pytest.mark.asyncio
async def test_reviewed_proof_queue_claim_and_mutation_fence(env):
    controller, client, _, _, repo = env
    request = request_for(repo)
    async def deposit(payload):
        binding = verify(base64.b64decode(payload['source_zip_base64']),
                         payload['source_commit_object_base64'], payload['source_commit'])
        client.binding = binding
        client.bindings[payload['source_id']] = binding
        return {k: payload[k] for k in ('source_id', 'project', 'source_digest')} | {
            'size_bytes': len(base64.b64decode(payload['source_zip_base64'])), 'source_binding': binding}
    client.put_source = deposit
    original_submit = client.submit
    async def submit(payload):
        with pytest.raises(HubError, match='active_work_job'):
            repo.update_context('human:1', 'task', request['review_task_id'],
                                request['review_task_revision'], 'changed during dispatch', [])
        result = await original_submit(payload)
        result['result']['source_commit_verification'] = 'git-sha1-tree-v1'
        result['result']['source_binding'] = client.binding
        return result
    client.submit = submit
    queued = await controller.submit('codex', request)
    assert queued['source_binding'] == client.binding
    with pytest.raises(HubError, match='active_work_job'):
        repo.set_candidate('codex', request['review_task_id'], request['review_task_revision'], 'f' * 40)
    done = await controller.run_once()
    assert done['job']['state'] == 'succeeded'
    assert client.posts == 1


@pytest.mark.asyncio
async def test_stale_review_denied_before_deposit(env):
    controller, client, _, _, repo = env
    request = request_for(repo)
    repo.set_candidate('codex', request['review_task_id'], request['review_task_revision'], 'f' * 40)
    with pytest.raises(HubError, match='staging_review_required'):
        await controller.submit('codex', request)
    assert client.deposits == 0


@pytest.mark.asyncio
async def test_mismatched_deposit_receipt_never_enqueues(env):
    controller, client, _, _, repo = env
    request = request_for(repo)
    async def deposit(payload):
        binding = verify(base64.b64decode(payload['source_zip_base64']),
                         payload['source_commit_object_base64'], payload['source_commit'])
        binding['tree_oid'] = 'f' * 40
        return {k: payload[k] for k in ('source_id', 'project', 'source_digest')} | {
            'size_bytes': 1, 'source_binding': binding}
    client.put_source = deposit
    with pytest.raises(HubError, match='staging_source_binding_receipt_mismatch'):
        await controller.submit('codex', request)
    with repo._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM hub_work_jobs').fetchone()[0] == 0
    assert client.posts == 0


@pytest.mark.asyncio
async def test_review_change_during_deposit_vetoes_queue_transaction(env):
    controller, client, _, _, repo = env
    request = request_for(repo)
    async def deposit(payload):
        binding = verify(base64.b64decode(payload['source_zip_base64']),
                         payload['source_commit_object_base64'], payload['source_commit'])
        repo.set_candidate('codex', request['review_task_id'], request['review_task_revision'], 'f' * 40)
        return {k: payload[k] for k in ('source_id', 'project', 'source_digest')} | {
            'size_bytes': 1, 'source_binding': binding}
    client.put_source = deposit
    with pytest.raises(HubError, match='work_target_not_verified'):
        await controller.submit('codex', request)
    with repo._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM hub_work_jobs').fetchone()[0] == 0
    assert client.posts == 0


@pytest.mark.asyncio
async def test_legacy_unreviewed_row_is_never_claimed(env):
    controller, client, _, _, repo = env
    request = request_for(repo)
    async def deposit(payload):
        binding = verify(base64.b64decode(payload['source_zip_base64']),
                         payload['source_commit_object_base64'], payload['source_commit'])
        return {k: payload[k] for k in ('source_id', 'project', 'source_digest')} | {
            'size_bytes': 1, 'source_binding': binding}
    client.put_source = deposit
    queued = await controller.submit('codex', request)
    with repo._transaction() as db:
        row = db.execute('SELECT data FROM hub_staging_submissions WHERE id=?',
                         (queued['job']['target']['submission_id'],)).fetchone()
        import json
        item = json.loads(row['data'])
        del item['review_task_revision']
        db.execute('UPDATE hub_staging_submissions SET data=? WHERE id=?',
                   (json.dumps(item), item['id']))
    assert await controller.run_once() is None
    assert controller.queue.get_job(queued['job']['id'])['state'] == 'queued'
    assert client.posts == 0


@pytest.mark.asyncio
async def test_failed_transactional_candidate_does_not_starve_other_machine(env, monkeypatch):
    controller, client, _, _, repo = env
    request = request_for(repo)
    async def deposit(payload):
        binding = verify(base64.b64decode(payload['source_zip_base64']),
                         payload['source_commit_object_base64'], payload['source_commit'])
        return {k: payload[k] for k in ('source_id', 'project', 'source_digest')} | {
            'size_bytes': 1, 'source_binding': binding}
    client.put_source = deposit
    await controller.submit('codex', request)
    queue = controller.queue
    queue.actions['other'] = WorkAction('other-machine', project='engineering-hub')
    original_target = queue.target_verifier
    queue.target_verifier = lambda actor, action, environment, target: (
        True if action == 'other' else original_target(actor, action, environment, target))
    other = queue.submit(actor='codex', request_id='other', action_key='other', environment='staging',
        target=WorkTarget(str(uuid.uuid4()), 'a' * 40, 'sha256:' + 'b' * 64))
    original_guard = queue._modular_target_in_transaction
    monkeypatch.setattr(queue, '_modular_target_in_transaction',
        lambda db, actor, action, environment, target: False if action.startswith('staging-service.')
        else original_guard(db, actor, action, environment, target))
    claimed = queue.claim(worker=controller.worker)
    assert claimed['id'] == other['id']


@pytest.mark.asyncio
async def test_revocation_after_live_claim_vetoes_dispatch_intent(env, monkeypatch):
    controller, client, resources, _, repo = env
    request = request_for(repo)
    async def deposit(payload):
        binding = verify(base64.b64decode(payload['source_zip_base64']),
                         payload['source_commit_object_base64'], payload['source_commit'])
        return {k: payload[k] for k in ('source_id', 'project', 'source_digest')} | {
            'size_bytes': 1, 'source_binding': binding}
    client.put_source = deposit
    queued = await controller.submit('codex', request)
    live_claim = controller.queue.live_claim
    def revoke_after_check(**kwargs):
        result = live_claim(**kwargs)
        resources.set_policy('human:1', 'engineering-hub', 1, {'codex': ['read']})
        return result
    monkeypatch.setattr(controller.queue, 'live_claim', revoke_after_check)
    await controller.run_once()
    assert client.posts == 0
    assert controller.queue.get_job(queued['job']['id'])['state'] == 'failed'


@pytest.mark.asyncio
async def test_revocation_after_intent_lost_ack_keeps_original_held(env):
    controller, client, resources, _, repo = env
    request = request_for(repo)
    async def deposit(payload):
        binding = verify(base64.b64decode(payload['source_zip_base64']),
                         payload['source_commit_object_base64'], payload['source_commit'])
        return {k: payload[k] for k in ('source_id', 'project', 'source_digest')} | {
            'size_bytes': 1, 'source_binding': binding}
    client.put_source = deposit
    async def revoke_and_lose_ack(payload):
        client.posts += 1
        resources.set_policy('human:1', 'engineering-hub', 1, {'codex': ['read']})
        raise TimeoutError()
    client.submit = revoke_and_lose_ack
    queued = await controller.submit('codex', request)
    result = await controller.run_once()
    assert client.posts == 1
    assert result['job']['id'] == queued['job']['id']
    assert result['job']['state'] == 'held'
    assert await controller.run_once() is None
    assert client.posts == 1


@pytest.mark.asyncio
async def test_failed_terminal_with_mismatched_binding_stays_held(env):
    controller, client, _, _, repo = env
    request = request_for(repo)
    async def deposit(payload):
        binding = verify(base64.b64decode(payload['source_zip_base64']),
                         payload['source_commit_object_base64'], payload['source_commit'])
        client.bindings[payload['source_id']] = binding
        return {k: payload[k] for k in ('source_id', 'project', 'source_digest')} | {
            'size_bytes': 1, 'source_binding': binding}
    client.put_source = deposit
    async def failed_with_wrong_binding(payload):
        client.posts += 1
        wrong = dict(client.bindings[payload['source_id']], tree_oid='f' * 40)
        return {'job_id': payload['job_id'], 'command_id': payload['command_id'], 'state': 'failed',
                'result': {'error': 'stage_test_failed', 'source_commit': payload['source_commit'],
                    'source_digest': payload['source_digest'], 'source_commit_verification': 'git-sha1-tree-v1',
                    'source_binding': wrong}}
    client.submit = failed_with_wrong_binding
    queued = await controller.submit('codex', request)
    result = await controller.run_once()
    assert result['job']['id'] == queued['job']['id']
    assert result['job']['state'] == 'held'
    assert client.posts == 1
