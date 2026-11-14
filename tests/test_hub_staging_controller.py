import base64
import asyncio
import hashlib
import io
import json
import os
import signal
import sys
import uuid
import zipfile

import pytest

from engineering_hub.projects import ProjectRepository
from engineering_hub.repository import HubRepository, HubError
from engineering_hub.staging_resources import StagingResourceStore
from engineering_hub.staging_client import StagingNotAccepted
from engineering_hub.staging_controller import StagingController, compose_queue
from engineering_hub.work_queue import WorkAction, WorkQueue, WorkTarget, WorkWorker
from engineering_hub.staging_client import StagingNotAccepted


class Client:
    def __init__(self):
        self.bindings = {}
        self.jobs, self.facts, self.posts, self.deposits = {}, {}, 0, 0
        self.lost_ack, self.cleanup_lost = False, False
        self.cleanup_receipts, self.cleanup_posts = {}, 0
        self.fail = False
        self.bytes = b'<html>demo</html>'

    async def status(self):
        return {'projects': {'engineering-hub': ['static'], 'landscripting': ['static']}}

    async def put_source(self, payload):
        self.deposits += 1
        from hub_staging_service.source_provenance import verify
        binding = verify(base64.b64decode(payload['source_zip_base64']), payload['source_commit_object_base64'], payload['source_commit'])
        self.bindings[payload['source_id']] = binding
        return {key: payload[key] for key in ('source_id', 'project', 'source_digest')} | {'size_bytes': 1, 'source_binding': binding}

    async def submit(self, payload):
        self.posts += 1
        result = {key: payload[key] for key in ('source_commit', 'source_digest', 'resource_ids')}
        result.update(source_commit_verification='git-sha1-tree-v1', source_binding=self.bindings[payload['source_id']], preview_ready=True,
                      stages={stage: {'exit_code': 0, 'log_bytes': 0} for stage in ('build', 'test')})
        receipt = {'job_id': payload['job_id'], 'command_id': payload['command_id'], 'state': 'succeeded', 'result': result}
        self.jobs[payload['job_id']] = receipt
        if self.fail:
            receipt.update(state='failed', result={'error': 'stage_test_failed',
                'source_commit': payload['source_commit'], 'source_digest': payload['source_digest'],
                'source_commit_verification': 'git-sha1-tree-v1', 'source_binding': self.bindings[payload['source_id']]})
        for kind, identity in payload['resource_ids'].items():
            manifest = {'index.html': {'bytes': len(self.bytes), 'sha256': hashlib.sha256(self.bytes).hexdigest()}}
            self.facts[identity] = {'id': identity, 'job_id': payload['job_id'], 'kind': kind, 'state': 'ready',
                                    'revision': 1, 'inode': 123, 'manifest': manifest, 'bytes': len(self.bytes), 'refs': []}
        if self.lost_ack:
            raise TimeoutError('private transport detail')
        return receipt

    async def job(self, identity):
        return self.jobs[identity]

    async def resource(self, identity):
        return dict(self.facts[identity])

    async def file(self, identity, path):
        return self.bytes, 'text/html'

    async def cleanup(self, payload):
        self.cleanup_posts += 1
        for wanted in payload['resources']:
            self.facts[wanted['resource_id']].update(state='deleted', revision=2, manifest={}, bytes=0)
        result = {'command_id': payload['command_id'], 'job_id': None, 'state': 'succeeded',
                  'result': {'deleted_resource_ids': [item['resource_id'] for item in payload['resources']]}}
        self.cleanup_receipts[payload['command_id']] = result
        if self.cleanup_lost:
            raise TimeoutError()
        return result

    async def operation(self, command):
        return self.cleanup_receipts[command]

    async def aclose(self):
        pass


def payload(repo, project='engineering-hub'):
    from hub_staging_service.source_provenance import _archive_tree, _object_id
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as bundle:
        info = zipfile.ZipInfo('index.html'); info.create_system = 3; info.external_attr = 0o100644 << 16
        bundle.writestr(info, '<html>source</html>')
    raw = stream.getvalue()
    tree = _archive_tree(raw)[0]
    obj = (f'tree {tree}\nauthor Test <test@example.invalid> 1 +0000\n'
           f'committer Test <test@example.invalid> 1 +0000\n\nfixture\n').encode()
    commit = _object_id(b'commit', obj)
    task = repo.create_task('human:1', project, 'fixture', str(uuid.uuid4()))
    task = repo.claim_task('codex', task['id'], task['revision'])
    task = repo.set_candidate('codex', task['id'], task['revision'], commit)
    task = repo.review_task('claude', task['id'], task['revision'], commit, 'approve')
    return {'request_id': str(uuid.uuid4()), 'review_task_id': task['id'], 'review_task_revision': task['revision'],
            'project': project, 'recipe': 'static', 'source_commit': commit,
            'source_digest': hashlib.sha256(raw).hexdigest(), 'source_zip_base64': base64.b64encode(raw).decode(),
            'source_commit_object_base64': base64.b64encode(obj).decode()}


@pytest.fixture
def env(tmp_path):
    repo = HubRepository(tmp_path / 'hub.db')
    projects = ProjectRepository(repo)
    projects.create_project('human:1', 'landscripting', 'LandScript', 'new')
    principal, now = object(), [1800000000]
    resources = StagingResourceStore(repo, admin_actors=frozenset({'human:1'}), control_principal=principal, clock=lambda: now[0])
    for project in ('engineering-hub', 'landscripting'):
        resources.set_policy('human:1', project, 0, {'codex': ['read', 'build', 'test', 'cleanup', 'preview'], 'human:1': ['read', 'cleanup', 'preview']})
    queue, actions = compose_queue(repo, None, {'engineering-hub': ['static'], 'landscripting': ['static']}, 'staging-pc')
    client = Client()
    controller = StagingController(repo, queue, client, resources=resources, control_principal=principal,
                                   actions=actions, poll_interval=0.001, max_poll_seconds=0.1)
    return controller, client, resources, now, repo


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['missing', 'uncertain', 'unknown', 'preview', 'build', 'test', 'build_failed_before_test', 'extra'])
async def test_contradictory_failed_receipt_keeps_original_reservation(env, case):
    controller, client, resources, _, repo = env
    original = client.submit

    async def contradict(value):
        receipt = await original(value)
        receipt['state'] = 'failed'
        result = receipt['result']
        result.pop('resource_ids')
        result['error'] = 'stage_build_failed' if case == 'build' else 'stage_test_failed'
        if case == 'missing':
            del result['error']
            result.pop('preview_ready')
            result.pop('stages')
        elif case == 'uncertain':
            result['error'] = 'execution_uncertain'
            result.pop('preview_ready')
            result.pop('stages')
        elif case == 'unknown':
            result['error'] = 'unknown_future_reason'
            result.pop('preview_ready')
            result.pop('stages')
        elif case in ('build', 'test'):
            result.pop('preview_ready')
        elif case == 'build_failed_before_test':
            result.pop('preview_ready')
            result['stages']['build']['exit_code'] = 1
            result['stages']['test']['exit_code'] = 1
        elif case == 'extra':
            result.pop('preview_ready')
            result.pop('stages')
            result['extra'] = 'unexpected'
        return receipt

    client.submit = contradict
    submitted = await controller.submit('codex', payload(repo))
    held = await controller.run_once()
    assert held['job']['state'] == 'held'
    assert controller.queue.get_job(submitted['job']['id'])['state'] == 'held'
    assert all(resources.get_resource('codex', identity)['state'] == 'provisioning'
               for identity in submitted['resource_ids'].values())
    with repo._connection() as db:
        reservation = db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=?', ('staging-pc',)).fetchone()
    assert reservation is not None and reservation['job_id'] == submitted['job']['id']
    assert (await controller.reconcile(submitted['job']['id']))['job']['state'] == 'held'
    assert client.posts == 1


@pytest.mark.asyncio
async def test_success_with_failure_reason_is_held_before_finalization(env):
    controller, client, resources, _, repo = env
    original = client.submit

    async def contradict(payload):
        receipt = await original(payload)
        receipt['result']['error'] = 'stage_test_failed'
        return receipt

    client.submit = contradict
    submitted = await controller.submit('codex', payload(repo))
    result = await controller.run_once()
    assert result['job']['state'] == 'held'
    assert controller.queue.get_job(submitted['job']['id'])['state'] == 'held'
    for identity in submitted['resource_ids'].values():
        assert resources.get_resource('codex', identity)['state'] == 'provisioning'
    assert client.posts == 1
    recovered = await controller.reconcile(submitted['job']['id'])
    assert recovered['job']['state'] == 'held'
    assert client.posts == 1
    with repo._connection() as db:
        reservation = db.execute(
            'SELECT job_id FROM hub_work_machines WHERE machine_id=?', ('staging-pc',)
        ).fetchone()
    assert reservation is not None and reservation['job_id'] == submitted['job']['id']


@pytest.mark.asyncio
async def test_deposit_queue_build_proof_and_preview_bytes(env):
    repo = env[4]
    controller, client, resources, _, _ = env
    submitted = await controller.submit('codex', payload(repo, 'landscripting'))
    assert submitted['job']['state'] == 'queued'
    assert client.posts == 0
    finished = await controller.run_once()
    assert finished['job']['state'] == 'succeeded'
    preview = submitted['resource_ids']['preview']
    assert resources.get_resource('codex', preview)['artifact_digest'].startswith('sha256:')
    assert await controller.preview_file(preview, 'index.html') == (client.bytes, 'text/html')
    client.bytes = b'changed'
    with pytest.raises(HubError, match='staging_preview_bytes_changed'):
        await controller.preview_file(preview, 'index.html')


@pytest.mark.asyncio
async def test_idempotency_blob_not_persisted_and_changed_request_denied(env):
    repo = env[4]
    controller, client, _, _, repo = env
    request = payload(repo)
    first = await controller.submit('codex', request)
    assert (await controller.submit('codex', request))['job']['id'] == first['job']['id']
    assert client.deposits == 1
    with repo._connection() as db:
        assert 'source_zip_base64' not in db.execute('SELECT data FROM hub_staging_submissions').fetchone()[0]
    request['source_commit'] = 'b' * 40
    with pytest.raises(HubError, match='staging_request_conflict'):
        await controller.submit('codex', request)


@pytest.mark.asyncio
async def test_revoked_test_grant_prevents_claim(env):
    repo = env[4]
    controller, client, resources, _, _ = env
    submitted = await controller.submit('codex', payload(repo))
    resources.set_policy('human:1', 'engineering-hub', 1, {'codex': ['read', 'build']})
    assert await controller.run_once() is None
    assert client.posts == 0
    assert controller.queue.get_job(submitted['job']['id'])['state'] == 'queued'


@pytest.mark.asyncio
async def test_lost_acceptance_holds_then_original_get_reconciles_without_repost(env):
    repo = env[4]
    controller, client, _, _, _ = env
    submitted = await controller.submit('codex', payload(repo))
    client.lost_ack = True
    held = await controller.run_once()
    assert held['job']['state'] == 'held'
    assert client.posts == 1
    finished = await controller.reconcile(submitted['job']['id'])
    assert finished['job']['state'] == 'succeeded'
    assert client.posts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('failed,drift', [(False, False), (False, True), (True, False), (True, True)])
async def test_partial_finalization_revalidates_previously_recorded_resources(env, monkeypatch, failed, drift):
    controller, client, resources, _, repo = env
    client.fail = failed
    original_submit = client.submit
    if failed:
        async def submit_with_diagnostic(request):
            receipt = await original_submit(request)
            identity = request['resource_ids']['test_run']
            fact = client.facts[identity]
            receipt['result']['diagnostic'] = {
                'resource_id': identity, 'bytes': fact['bytes'], 'stage': 'test',
                'manifest_sha256': hashlib.sha256(json.dumps(fact['manifest'], sort_keys=True,
                    separators=(',', ':')).encode()).hexdigest(),
                'scope': 'original_engine_owned_writers_exporters_volumes_absent',
            }
            return receipt
        monkeypatch.setattr(client, 'submit', submit_with_diagnostic)
    submitted = await controller.submit('codex', payload(repo))
    selected = submitted['resource_ids']['test_run' if failed else 'build']
    original_mark = resources.mark_resource_ready
    def interrupt_after_record(principal, identity, revision, **kwargs):
        result = original_mark(principal, identity, revision, **kwargs)
        if identity == selected:
            raise RuntimeError('synthetic interruption after committed resource record')
        return result
    monkeypatch.setattr(resources, 'mark_resource_ready', interrupt_after_record)
    held = await controller.run_once()
    assert held['job']['state'] == 'held' and client.posts == 1
    monkeypatch.setattr(resources, 'mark_resource_ready', original_mark)
    before = {identity: resources.inspect_resource(controller.principal, identity)
              for identity in submitted['resource_ids'].values()}
    assert before[selected]['state'] == ('failed' if failed else 'ready')
    if drift:
        fact = client.facts[selected]
        fact['manifest'] = {'changed.txt': {'bytes': 3, 'sha256': hashlib.sha256(b'new').hexdigest()}}
        fact['bytes'] = 3
        if failed:
            diagnostic = client.jobs[submitted['job']['id']]['result']['diagnostic']
            diagnostic['bytes'] = 3
            diagnostic['manifest_sha256'] = hashlib.sha256(json.dumps(fact['manifest'], sort_keys=True,
                separators=(',', ':')).encode()).hexdigest()
    resumed = await controller.reconcile(submitted['job']['id'])
    assert client.posts == 1  # Original command is inspected, never reposted.
    if drift:
        assert resumed['job']['state'] == 'held'
        assert {identity: resources.inspect_resource(controller.principal, identity)
                for identity in submitted['resource_ids'].values()} == before
        with repo._connection() as db:
            machine = db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=?', ('staging-pc',)).fetchone()
        assert machine['job_id'] == submitted['job']['id']
    else:
        assert resumed['job']['state'] == ('failed' if failed else 'succeeded')
@pytest.mark.asyncio
async def test_same_machine_existing_production_priority_and_verifiers_preserved(tmp_path):
    repo = HubRepository(tmp_path / 'hub.db')
    original = WorkQueue(repo, {'production': WorkAction('same-host', frozenset({'production'}), project='engineering-hub')},
                         target_verifier=lambda *args: True, approval_verifier=lambda *args: True)
    target = WorkTarget(str(uuid.uuid4()), 'a' * 40, 'sha256:' + 'b' * 64)
    production = original.submit(actor='codex', request_id='prod', action_key='production', environment='production', target=target, approval_id='approved')
    queue, actions = compose_queue(repo, original, {'engineering-hub': ['static']}, 'same-host')
    principal = object()
    resources = StagingResourceStore(repo, admin_actors=frozenset({'human:1'}), control_principal=principal)
    resources.set_policy('human:1', 'engineering-hub', 0, {'codex': ['read', 'build', 'test']})
    controller = StagingController(repo, queue, Client(), resources=resources, control_principal=principal, actions=actions)
    await controller.submit('codex', payload(repo))
    assert await controller.run_once() is None
    assert queue.get_job(production['id'])['state'] == 'queued'
    assert queue.target_verifier('codex', 'production', 'production', target) is True


@pytest.mark.asyncio
async def test_cleanup_exact_receipt_tombstone_and_uncertainty_never_replayed(env):
    repo = env[4]
    controller, client, resources, now, _ = env
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    now[0] += 32 * 86400
    plan = resources.cleanup_plan('codex', 'engineering-hub', kinds=['preview'])
    result = await controller.cleanup_apply('codex', plan['plan_id'], plan['digest'])
    assert result['removed'] == [submitted['resource_ids']['preview']]
    assert (await controller.cleanup_apply('codex', plan['plan_id'], plan['digest']))['removed'] == result['removed']
    plan = resources.cleanup_plan('codex', 'engineering-hub', kinds=['build'])
    client.cleanup_lost = True
    result = await controller.cleanup_apply('codex', plan['plan_id'], plan['digest'])
    assert result['failed']
    assert resources.get_resource('codex', submitted['resource_ids']['build'])['state'] == 'deleting'
    posts = client.cleanup_posts
    reconciled = await controller.cleanup_apply('codex', plan['plan_id'], plan['digest'])
    assert reconciled['removed'] == [submitted['resource_ids']['build']]
    assert client.cleanup_posts == posts


def human_cleanup_guard(repo, sessions):
    from starlette.requests import Request
    from engineering_hub.auth import AuthConfig, human_transaction_guard
    config = AuthConfig('https://hub.test', beau_github_id=1)
    session = sessions.create_session(1)
    scope = {'type': 'http', 'method': 'POST', 'path': '/', 'headers': [
        (b'origin', config.origin.encode()),
        (b'x-hub-csrf', session['csrf_token'].encode()),
        (b'cookie', f"{config.cookie_name}={session['cookie_token']}".encode()),
    ]}
    request = Request(scope)
    return session, human_transaction_guard(request, config, sessions, 'human:1')


@pytest.mark.asyncio
async def test_human_cleanup_logout_while_waiting_for_lock_rolls_back_admission(env):
    from fastapi import HTTPException
    from engineering_hub.auth import SessionStore
    controller, client, resources, now, repo = env
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    now[0] += 32 * 86400
    identity = submitted['resource_ids']['preview']
    plan = resources.cleanup_plan('human:1', 'engineering-hub', kinds=['preview'])
    sessions = SessionStore(repo.path, repository=repo)
    session, guard = human_cleanup_guard(repo, sessions)
    await controller.lock.acquire()
    task = asyncio.create_task(controller.cleanup_apply('human:1', plan['plan_id'], plan['digest'], admission_guard=guard))
    await asyncio.sleep(0)
    sessions.delete_session(session['cookie_token'])
    controller.lock.release()
    with pytest.raises(HTTPException) as error:
        await task
    assert error.value.status_code == 401
    assert client.cleanup_posts == 0
    assert resources.get_resource('human:1', identity)['state'] == 'ready'


@pytest.mark.parametrize('expire', [False, True])
@pytest.mark.asyncio
async def test_human_cleanup_revoked_during_preflight_never_dispatches(env, expire):
    from engineering_hub.auth import SessionStore
    controller, client, resources, now, repo = env
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    now[0] += 32 * 86400
    identity = submitted['resource_ids']['preview']
    plan = resources.cleanup_plan('human:1', 'engineering-hub', kinds=['preview'])
    sessions = SessionStore(repo.path, repository=repo)
    session, guard = human_cleanup_guard(repo, sessions)
    original = client.resource
    async def revoked_before_dispatch(resource_id):
        fact = await original(resource_id)
        if expire:
            with sessions._db() as db:
                db.execute('UPDATE auth_sessions SET expires=0')
        else:
            sessions.delete_session(session['cookie_token'])
        return fact
    client.resource = revoked_before_dispatch
    result = await controller.cleanup_apply('human:1', plan['plan_id'], plan['digest'], admission_guard=guard)
    assert result['failed'] == [{'id': identity, 'code': 'not_dispatched'}]
    assert client.cleanup_posts == 0
    assert resources.get_resource('human:1', identity)['state'] == 'ready'


@pytest.mark.asyncio
async def test_human_cleanup_admitted_before_logout_may_finish_without_repost(env):
    from engineering_hub.auth import SessionStore
    controller, client, resources, now, repo = env
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    now[0] += 32 * 86400
    identity = submitted['resource_ids']['preview']
    plan = resources.cleanup_plan('human:1', 'engineering-hub', kinds=['preview'])
    sessions = SessionStore(repo.path, repository=repo)
    session, guard = human_cleanup_guard(repo, sessions)
    original = client.cleanup
    async def logout_after_dispatch_intent(data):
        sessions.delete_session(session['cookie_token'])
        return await original(data)
    client.cleanup = logout_after_dispatch_intent
    result = await controller.cleanup_apply('human:1', plan['plan_id'], plan['digest'], admission_guard=guard)
    assert result['removed'] == [identity] and client.cleanup_posts == 1
    _, fresh_guard = human_cleanup_guard(repo, sessions)
    replay = await controller.cleanup_apply('human:1', plan['plan_id'], plan['digest'], admission_guard=fresh_guard)
    assert replay['removed'] == [identity] and client.cleanup_posts == 1


@pytest.mark.asyncio
async def test_human_cleanup_http_wires_guard_through_preflight(env):
    import httpx
    from fastapi import FastAPI
    from engineering_hub.auth import AuthConfig, SessionStore, human_transaction_guard, require_human
    from engineering_hub.staging_api import attach_staging_routes
    controller, client, resources, now, repo = env
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    now[0] += 32 * 86400
    identity = submitted['resource_ids']['preview']
    plan = resources.cleanup_plan('human:1', 'engineering-hub', kinds=['preview'])
    sessions = SessionStore(repo.path, repository=repo)
    session = sessions.create_session(1)
    config = AuthConfig('https://hub.test', beau_github_id=1)
    app = FastAPI()
    def human(request, mutation=False):
        return require_human(request, config, sessions, mutation=mutation)
    attach_staging_routes(app, controller, human, human,
        human_admission=lambda request, principal: human_transaction_guard(request, config, sessions, principal))
    original = client.resource
    async def logout_during_preflight(resource_id):
        fact = await original(resource_id)
        sessions.delete_session(session['cookie_token'])
        return fact
    client.resource = logout_during_preflight
    headers = {'origin': config.origin, 'x-hub-csrf': session['csrf_token'],
               'cookie': f"{config.cookie_name}={session['cookie_token']}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=config.origin) as http:
        response = await http.post('/api/hub/v1/staging/cleanup/apply', headers=headers,
            json={'plan_id': plan['plan_id'], 'digest': plan['digest']})
    assert response.status_code == 200
    assert response.json()['failed'] == [{'id': identity, 'code': 'not_dispatched'}]
    assert client.cleanup_posts == 0


@pytest.mark.parametrize('failure', ['timeout', 'referenced', 'wrong-job'])
@pytest.mark.asyncio
async def test_cleanup_preflight_failure_restores_with_receipt_and_requires_new_plan(env, failure):
    repo = env[4]
    controller, client, resources, now, repo = env
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    now[0] += 32 * 86400
    identity = submitted['resource_ids']['preview']
    before = resources.get_resource('codex', identity)
    plan = resources.cleanup_plan('codex', 'engineering-hub', kinds=['preview'])
    original = client.resource
    reads = []
    async def failing(resource_id):
        reads.append(resource_id)
        if failure == 'timeout':
            raise TimeoutError('private preflight failure')
        fact = await original(resource_id)
        fact.update(refs=['new-reference'] if failure == 'referenced' else [],
                    job_id=str(uuid.uuid4()) if failure == 'wrong-job' else fact['job_id'])
        return fact
    client.resource = failing
    result = await controller.cleanup_apply('codex', plan['plan_id'], plan['digest'])
    assert result['failed'] == [{'id': identity, 'code': 'not_dispatched'}]
    restored = resources.get_resource('codex', identity)
    assert restored['state'] == before['state'] == 'ready'
    assert restored['revision'] == before['revision'] + 2 and client.cleanup_posts == 0
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_staging_cleanup_receipts').fetchone()[0] == 1
    replay = await controller.cleanup_apply('codex', plan['plan_id'], plan['digest'])
    assert replay['failed'] == result['failed'] and len(reads) == 1 and client.cleanup_posts == 0
    client.resource = original
    fresh = resources.cleanup_plan('codex', 'engineering-hub', kinds=['preview'])
    assert fresh['plan_id'] != plan['plan_id'] and fresh['digest'] != plan['digest']
    assert (await controller.cleanup_apply('codex', fresh['plan_id'], fresh['digest']))['removed'] == [identity]
    assert client.cleanup_posts == 1


@pytest.mark.asyncio
async def test_cleanup_preflight_cancellation_restores_without_post_and_replay_does_not_retry(env):
    repo = env[4]
    controller, client, resources, now, _ = env
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    now[0] += 32 * 86400
    identity = submitted['resource_ids']['preview']
    plan = resources.cleanup_plan('codex', 'engineering-hub', kinds=['preview'])
    entered = asyncio.Event()
    async def stalled(resource_id):
        entered.set()
        await asyncio.Event().wait()
    client.resource = stalled
    caller = asyncio.create_task(controller.cleanup_apply('codex', plan['plan_id'], plan['digest']))
    await entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert client.cleanup_posts == 0
    assert resources.get_resource('codex', identity)['state'] == 'ready'
    assert (await controller.cleanup_apply('codex', plan['plan_id'], plan['digest']))['failed'] == [
        {'id': identity, 'code': 'not_dispatched'}]


@pytest.mark.parametrize('with_journal', [False, True])
@pytest.mark.asyncio
async def test_replayed_preflight_after_restart_restores_without_worker_io(env, with_journal):
    repo = env[4]
    controller, client, resources, now, repo = env
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    now[0] += 32 * 86400
    identity = submitted['resource_ids']['preview']
    plan = resources.cleanup_plan('codex', 'engineering-hub', kinds=['preview'])
    operation = resources.cleanup_begin('codex', plan['plan_id'], plan['digest'])['operations'][0]
    if with_journal:
        with repo._transaction() as db:
            db.execute('INSERT INTO hub_staging_dispatch_cleanup VALUES(?,?)', (operation['operation_id'],
                json.dumps(dict(state='preflight', plan_id=plan['plan_id'], resource_id=identity,
                                resource_revision=operation['expected_revision']))))
    async def forbidden(*args):
        pytest.fail('Replayed preflight must not contact worker or POST')
    client.resource = client.operation = client.cleanup = forbidden
    result = await controller.cleanup_apply('codex', plan['plan_id'], plan['digest'])
    assert result['failed'] == [{'id': identity, 'code': 'not_dispatched'}]
    assert resources.get_resource('codex', identity)['state'] == 'ready'


@pytest.mark.parametrize('reason', ['worker_busy', 'cleanup_revision_or_reference', 'resource_job_not_terminal'])
@pytest.mark.asyncio
async def test_cleanup_definitive_first_post_rejection_restores_but_is_not_not_dispatched(env, reason):
    repo = env[4]
    controller, client, resources, now, repo = env
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    now[0] += 32 * 86400
    identity = submitted['resource_ids']['preview']
    plan = resources.cleanup_plan('codex', 'engineering-hub', kinds=['preview'])
    calls = []
    async def rejected(request):
        calls.append(request)
        raise StagingNotAccepted(reason)
    original = client.cleanup
    client.cleanup = rejected
    result = await controller.cleanup_apply('codex', plan['plan_id'], plan['digest'])
    assert result['failed'] == [{'id': identity, 'code': 'not_accepted'}]
    assert resources.get_resource('codex', identity)['state'] == 'ready'
    assert len(calls) == 1
    with repo._connection() as db:
        journal = json.loads(db.execute('SELECT data FROM hub_staging_dispatch_cleanup').fetchone()[0])
        assert journal['state'] == 'not_accepted' and journal['reason'] == reason
        assert journal['payload'] == calls[0]
        assert db.execute("SELECT count(*) FROM hub_audit WHERE action='staging.cleanup_not_dispatched'").fetchone()[0] == 0
    assert (await controller.cleanup_apply('codex', plan['plan_id'], plan['digest']))['failed'] == result['failed']
    assert len(calls) == 1
    client.cleanup = original
    fresh = resources.cleanup_plan('codex', 'engineering-hub', kinds=['preview'])
    assert (await controller.cleanup_apply('codex', fresh['plan_id'], fresh['digest']))['removed'] == [identity]


@pytest.mark.asyncio
async def test_unknown_post_error_is_not_reinterpreted_as_definitive_rejection(env):
    repo = env[4]
    controller, client, resources, now, _ = env
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    now[0] += 32 * 86400
    identity = submitted['resource_ids']['preview']
    plan = resources.cleanup_plan('codex', 'engineering-hub', kinds=['preview'])
    calls = []
    async def unknown(request):
        calls.append(request)
        raise TimeoutError('worker_busy text is not authenticated rejection proof')
    client.cleanup = unknown
    result = await controller.cleanup_apply('codex', plan['plan_id'], plan['digest'])
    assert result['failed'] == [{'id': identity, 'code': 'reconciliation_required'}]
    assert resources.get_resource('codex', identity)['state'] == 'deleting'
    assert (await controller.cleanup_apply('codex', plan['plan_id'], plan['digest']))['failed'] == result['failed']
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_humans_cannot_impersonate_queue_agent(env):
    repo = env[4]
    with pytest.raises(HubError, match='staging_agent_required'):
        await env[0].submit('human:1', payload(repo))


@pytest.mark.asyncio
async def test_explicit_start_close_without_worker_effects(env):
    repo = env[4]
    controller = env[0]
    assert (await controller.status('codex'))['running'] is False
    await controller.start()
    assert (await controller.status('codex'))['running'] is True
    await controller.close()


@pytest.mark.asyncio
async def test_unknown_approved_production_survives_registry_reload(tmp_path):
    repo = HubRepository(tmp_path / 'hub.db')
    old = WorkQueue(repo, {'retired-production': WorkAction('same-host', frozenset({'production'}), project='engineering-hub')},
                    target_verifier=lambda *args: True, approval_verifier=lambda *args: True)
    old.submit(actor='codex', request_id='old-prod', action_key='retired-production', environment='production',
               target=WorkTarget(str(uuid.uuid4()), 'a' * 40, 'sha256:' + 'b' * 64), approval_id='approved')
    queue, actions = compose_queue(repo, None, {'engineering-hub': ['static']}, 'same-host')
    principal = object()
    resources = StagingResourceStore(repo, admin_actors=frozenset({'human:1'}), control_principal=principal)
    resources.set_policy('human:1', 'engineering-hub', 0, {'codex': ['read', 'build', 'test']})
    client = Client()
    controller = StagingController(repo, queue, client, resources=resources, control_principal=principal, actions=actions)
    await controller.submit('codex', payload(repo))
    assert await controller.run_once() is None
    assert client.posts == 0


@pytest.mark.asyncio
async def test_new_failure_supersedes_only_old_latest_failure_protection(env):
    repo = env[4]
    controller, client, resources, now, _ = env
    client.fail = True
    first = await controller.submit('codex', payload(repo))
    assert (await controller.run_once())['job']['state'] == 'failed'
    old = resources.inspect_resource(controller.principal, first['resource_ids']['preview'])
    resources.set_protection(controller.principal, old['id'], old['revision'], old['protected'] +
                              [{'reason': 'decision_evidence', 'ref': 'retain-this'}])
    second = await controller.submit('codex', payload(repo))
    assert (await controller.run_once())['job']['state'] == 'failed'
    old = resources.get_resource('codex', first['resource_ids']['preview'])
    assert {'reason': 'decision_evidence', 'ref': 'retain-this'} in old['protected']
    assert not any(p['reason'] == 'latest_failure' for p in old['protected'])
    now[0] += 32 * 86400
    plan = resources.cleanup_plan('codex', 'engineering-hub', kinds=['test_run'])
    assert first['resource_ids']['test_run'] in {entry['id'] for entry in plan['remove']}
    assert second['resource_ids']['test_run'] not in {entry['id'] for entry in plan['remove']}


@pytest.mark.asyncio
async def test_new_failure_does_not_revise_already_superseded_resources_with_uncertain_cleanup(env):
    repo = env[4]
    controller, client, resources, now, _ = env
    client.fail = True
    first = await controller.submit('codex', payload(repo))
    assert (await controller.run_once())['job']['state'] == 'failed'
    await controller.submit('codex', payload(repo))
    assert (await controller.run_once())['job']['state'] == 'failed'
    now[0] += 32 * 86400
    plan = resources.cleanup_plan('codex', 'engineering-hub', kinds=['test_run'])
    client.cleanup_lost = True
    assert (await controller.cleanup_apply('codex', plan['plan_id'], plan['digest']))['failed']
    old = resources.get_resource('codex', first['resource_ids']['test_run'])
    assert old['state'] == 'deleting'
    third = await controller.submit('codex', payload(repo))
    finished = await controller.run_once()
    assert finished['job']['state'] == 'failed' and finished['phase'] == 'verified'
    assert controller.queue.get_job(third['job']['id'])['state'] == 'failed'
    assert resources.get_resource('codex', old['id']) == old


@pytest.mark.asyncio
async def test_caller_cancellation_does_not_cancel_owned_dispatch_and_close_drains(env):
    repo = env[4]
    controller, client, _, _, _ = env
    entered, release = asyncio.Event(), asyncio.Event()
    original = client.submit
    async def blocked(request):
        entered.set()
        await release.wait()
        return await original(request)
    client.submit = blocked
    submitted = await controller.submit('codex', payload(repo))
    caller = asyncio.create_task(controller.run_once())
    await entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    closing = asyncio.create_task(controller.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await closing
    assert client.posts == 1
    assert controller.queue.get_job(submitted['job']['id'])['state'] == 'succeeded'


def test_archived_dynamic_profile_keeps_history_but_denies_new_admission(tmp_path):
    repo = HubRepository(tmp_path / 'archive.db')
    projects = ProjectRepository(repo)
    project = projects.create_project('human:1', 'client-notes', 'Client notes', 'create-client')
    actions = {'client-check': WorkAction('staging-pc', project='client-notes',
                                        requires_execution_proof=False)}
    queue = WorkQueue(repo, actions, target_verifier=lambda *args: True)
    target = WorkTarget(str(uuid.uuid4()), 'a' * 40, 'sha256:' + 'b' * 64)
    job = queue.submit(actor='codex', request_id='before-archive', action_key='client-check',
                       environment='staging', target=target)
    worker = WorkWorker('codex', str(uuid.uuid4()))
    claimed = queue.claim(worker=worker)
    queue.complete(job_id=job['id'], worker=worker, fence=claimed['fence'], outcome='succeeded')
    projects.archive_project('human:1', 'client-notes', project['revision'])
    reopened = WorkQueue(repo, actions, target_verifier=lambda *args: True)
    assert reopened.get_job(job['id'])['state'] == 'succeeded'
    assert reopened.claim(worker=WorkWorker('codex', str(uuid.uuid4()))) is None
    with pytest.raises(HubError, match='project_archived'):
        reopened.submit(actor='codex', request_id='after-archive', action_key='client-check',
                        environment='staging', target=target)


@pytest.mark.asyncio
async def test_owner_rechecks_original_receipt_without_rerun(env):
    repo = env[4]
    controller, client, _, _, _ = env
    client.lost_ack = True
    submitted = await controller.submit('codex', payload(repo))
    assert (await controller.run_once())['phase'] == 'held'
    with pytest.raises(HubError):
        await controller.reconcile_for('codex', submitted['job']['id'])
    with pytest.raises(HubError, match='staging_admin_required'):
        await controller.reconcile_for('human:2', submitted['job']['id'])
    result = await controller.reconcile_for('human:1', submitted['job']['id'], admission_guard=lambda db: None)
    assert result['job']['state'] == 'succeeded' and client.posts == 1
    assert (await controller.reconcile_for('human:1', submitted['job']['id'], admission_guard=lambda db: None))['job']['state'] == 'succeeded'
    assert client.posts == 1
    policy = controller.resources.get_policy('human:1', 'engineering-hub')
    controller.resources.set_policy('human:1', 'engineering-hub', policy['revision'],
                                    {'human:1': ['read']})
    assert (await controller.reconcile_for('human:1', submitted['job']['id'], admission_guard=lambda db: None))['job']['state'] == 'succeeded'
    assert client.posts == 1


@pytest.mark.asyncio
async def test_reconcile_terminal_view_failure_does_not_rewrite_verified_submission(env, monkeypatch):
    controller, client, _, _, _ = env
    client.lost_ack = True
    submitted = await controller.submit('codex', payload(env[4]))
    identity = submitted['job']['id']
    assert (await controller.run_once())['phase'] == 'held'
    original = controller.queue.get_job
    injected = False

    def fail_once_after_terminal(job_id):
        nonlocal injected
        result = original(job_id)
        if not injected and result['state'] == 'succeeded':
            injected = True
            raise OSError('synthetic terminal view read failure')
        return result

    monkeypatch.setattr(controller.queue, 'get_job', fail_once_after_terminal)
    with pytest.raises(OSError, match='synthetic terminal view read failure'):
        await controller.reconcile_for('human:1', identity, admission_guard=lambda db: None)
    assert injected
    assert original(identity)['state'] == 'succeeded'
    assert controller._read(job_id=identity)['phase'] == 'verified'
    assert client.posts == 1


@pytest.mark.asyncio
async def test_run_terminal_view_failure_does_not_rewrite_verified_submission(env, monkeypatch):
    controller, client, _, _, repo = env
    submitted = await controller.submit('codex', payload(repo))
    identity = submitted['job']['id']
    original = controller.queue.get_job
    injected = False

    def fail_once_after_terminal(job_id):
        nonlocal injected
        result = original(job_id)
        if not injected and result['state'] == 'succeeded':
            injected = True
            raise OSError('synthetic terminal view read failure')
        return result

    monkeypatch.setattr(controller.queue, 'get_job', fail_once_after_terminal)
    with pytest.raises(OSError, match='synthetic terminal view read failure'):
        await controller.run_once()
    assert injected
    assert original(identity)['state'] == 'succeeded'
    assert controller._read(job_id=identity)['phase'] == 'verified'
    assert client.posts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_point', ['verified_save', 'verified_save_committed', 'queue_reconcile'])
async def test_reconcile_finalization_error_preserves_retryable_state(env, monkeypatch, failure_point):
    controller, client, _, _, repo = env
    client.lost_ack = True
    submitted = await controller.submit('codex', payload(repo))
    identity = submitted['job']['id']
    assert (await controller.run_once())['phase'] == 'held'
    original_save = controller._save
    original_reconcile = controller.queue.reconcile

    def fail_save(item):
        if item.get('phase') == 'verified':
            if failure_point == 'verified_save_committed':
                original_save(item)
            raise OSError('synthetic verified save failure')
        return original_save(item)

    def fail_reconcile(**kwargs):
        raise OSError('synthetic queue reconciliation failure')

    with monkeypatch.context() as patch:
        if failure_point in ('verified_save', 'verified_save_committed'):
            patch.setattr(controller, '_save', fail_save)
        else:
            patch.setattr(controller.queue, 'reconcile', fail_reconcile)
        with pytest.raises(OSError, match='synthetic'):
            await controller.reconcile_for('human:1', identity, admission_guard=lambda db: None)
    assert controller.queue.get_job(identity)['state'] == 'held'
    assert controller._read(job_id=identity)['phase'] == ('held' if failure_point == 'verified_save' else 'verified')
    assert (await controller.reconcile_for('human:1', identity, admission_guard=lambda db: None))['job']['state'] == 'succeeded'
    assert client.posts == 1


@pytest.mark.asyncio
async def test_run_queue_completion_error_preserves_verified_for_reconciliation(env, monkeypatch):
    controller, client, _, _, repo = env
    submitted = await controller.submit('codex', payload(repo))
    identity = submitted['job']['id']

    def fail_complete(**kwargs):
        raise OSError('synthetic queue completion failure')

    with monkeypatch.context() as patch:
        patch.setattr(controller.queue, 'complete', fail_complete)
        with pytest.raises(OSError, match='synthetic queue completion failure'):
            await controller.run_once()
    assert controller.queue.get_job(identity)['state'] == 'claimed'
    assert controller._read(job_id=identity)['phase'] == 'verified'
    assert (await controller.reconcile_for('human:1', identity, admission_guard=lambda db: None))['job']['state'] == 'succeeded'
    assert client.posts == 1


@pytest.mark.asyncio
async def test_reconcile_competing_pause_during_worker_read_fails_without_rewriting(env):
    controller, client, _, _, repo = env
    client.lost_ack = True
    submitted = await controller.submit('codex', payload(repo))
    assert (await controller.run_once())['phase'] == 'held'
    before = controller._read(job_id=submitted['job']['id'])
    original = client.job

    async def change_during_read(identity):
        repo.set_pause('human:1', True)
        return await original(identity)

    client.job = change_during_read
    with pytest.raises(HubError, match='staging_reconciliation_changed'):
        await controller.reconcile_for('human:1', submitted['job']['id'], admission_guard=lambda db: None)
    assert controller._read(job_id=submitted['job']['id']) == before
    assert controller.queue.get_job(submitted['job']['id'])['state'] == 'held'
    assert client.posts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('drift', ['fence', 'lease', 'machine_generation', 'submission', 'resource'])
async def test_reconcile_generation_drift_during_remote_read_fails_closed(env, drift):
    controller, client, _, _, repo = env
    client.lost_ack = True
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    identity = submitted['job']['id']
    original = client.job
    async def change_during_read(job_id):
        with repo._transaction() as db:
            if drift == 'fence':
                db.execute('UPDATE hub_work_jobs SET fence=COALESCE(fence,0)+1 WHERE id=?', (identity,))
            elif drift == 'lease':
                db.execute('UPDATE hub_work_jobs SET lease_until=COALESCE(lease_until,0)+1 WHERE id=?', (identity,))
            elif drift == 'machine_generation':
                db.execute('UPDATE hub_work_machines SET generation=generation+1 WHERE machine_id=(SELECT machine_id FROM hub_work_jobs WHERE id=?)', (identity,))
            elif drift == 'submission':
                db.execute("UPDATE hub_staging_submissions SET fingerprint=fingerprint || 'changed' WHERE id=?", (controller._read(job_id=identity)['id'],))
            else:
                db.execute("UPDATE hub_entities SET data=json_set(data,'$.state','changed') WHERE id=?", (submitted['resource_ids']['build'],))
        return await original(job_id)
    client.job = change_during_read
    with pytest.raises(HubError, match='staging_reconciliation_changed'):
        await controller.reconcile_for('human:1', identity, admission_guard=lambda db: None)
    assert controller.queue.get_job(identity)['state'] == 'held'
    assert client.posts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['submission', 'target', 'resource'])
async def test_reconcile_corrupt_json_during_remote_read_does_not_mutate(env, kind):
    controller, client, _, _, repo = env
    client.lost_ack = True
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    identity = submitted['job']['id']
    submission_id = controller._read(job_id=identity)['id']
    original = client.job
    async def corrupt(job_id):
        with repo._transaction() as db:
            if kind == 'submission':
                db.execute("UPDATE hub_staging_submissions SET data='null' WHERE id=?", (submission_id,))
            elif kind == 'target':
                db.execute("UPDATE hub_work_jobs SET target='{' WHERE id=?", (identity,))
            else:
                db.execute("UPDATE hub_entities SET data='{' WHERE id=?", (submitted['resource_ids']['build'],))
        return await original(job_id)
    client.job = corrupt
    with pytest.raises(HubError, match='staging_reconciliation_changed'):
        await controller.reconcile_for('human:1', identity, admission_guard=lambda db: None)
    with repo._connection() as db:
        row = db.execute('SELECT state FROM hub_work_jobs WHERE id=?', (identity,)).fetchone()
        assert row[0] == 'held'
    assert client.posts == 1


@pytest.mark.asyncio
async def test_queued_reconcile_competes_with_real_claim(env, monkeypatch):
    from engineering_hub import reconciliation_context
    controller, client, _, _, repo = env
    submitted = await controller.submit('codex', payload(repo))
    identity = submitted['job']['id']
    other, actions = compose_queue(repo, None, {'engineering-hub': ['static'], 'landscripting': ['static']}, 'staging-pc')
    StagingController(repo, other, client, resources=controller.resources, control_principal=controller.principal, actions=actions, worker=controller.worker)
    original = controller._reconcile_locked
    async def claim_after_admission(job_id, *, item, current):
        with reconciliation_context.suspended():
            claimed = other.claim(worker=controller.worker)
        assert claimed['id'] == identity
        return await original(job_id, item=item, current=current)
    monkeypatch.setattr(controller, '_reconcile_locked', claim_after_admission)
    with pytest.raises(HubError):
        await controller.reconcile_for('human:1', identity, admission_guard=lambda db: None)
    job = other.get_job(identity)
    assert job['state'] == 'claimed' and client.posts == 0
    with repo._connection() as db:
        assert db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=?', (job['machine_id'],)).fetchone()[0] == identity


@pytest.mark.asyncio
@pytest.mark.parametrize('interleave', ['renew', 'expire'])
async def test_reconcile_competes_with_real_queue_lease_api(env, interleave):
    from engineering_hub import reconciliation_context
    controller, client, resources, _, repo = env
    clock = [1000.0]
    controller.queue.clock = lambda: clock[0]
    submitted = await controller.submit('codex', payload(repo))
    identity = submitted['job']['id']
    submit_original, job_original = client.submit, client.job
    entered = asyncio.Event()
    async def running(request):
        receipt = await submit_original(request)
        return dict(receipt, state='running')
    async def waiting(job_id):
        entered.set()
        await asyncio.Event().wait()
    client.submit, client.job = running, waiting
    task = asyncio.create_task(controller._run_once())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    item = controller._read(job_id=identity)
    claim = item['claim']
    other_queue, _ = compose_queue(repo, None, {'engineering-hub': ['static'], 'landscripting': ['static']}, 'staging-pc')
    other_queue.clock = lambda: clock[0]
    async def competing(job_id):
        with reconciliation_context.suspended():
            if interleave == 'renew':
                other_queue.renew(job_id=identity, worker=controller.worker, fence=claim['fence'], lease_seconds=120)
            else:
                clock[0] += 601
                assert other_queue.claim(worker=controller.worker) is None
        return await job_original(job_id)
    client.job = competing
    with pytest.raises(HubError, match='staging_reconciliation_changed'):
        await controller.reconcile_for('human:1', identity, admission_guard=lambda db: None)
    job = other_queue.get_job(identity)
    assert job['state'] == ('claimed' if interleave == 'renew' else 'held')
    with repo._connection() as db:
        assert db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=?', (job['machine_id'],)).fetchone()[0] == identity
    assert client.posts == 1


@pytest.mark.asyncio
async def test_two_controllers_reconcile_same_held_job_without_stale_rewrite(env):
    first, client, resources, _, repo = env
    client.lost_ack = True
    submitted = await first.submit('codex', payload(repo))
    await first.run_once()
    identity = submitted['job']['id']
    original = client.job
    entered, resume = asyncio.Event(), asyncio.Event()

    async def blocked_read(job_id):
        entered.set()
        await resume.wait()
        return await original(job_id)

    # A separate controller has an independent lock, but shares the ledger.
    other_client = Client()
    other_client.jobs, other_client.facts = client.jobs, client.facts
    other = StagingController(repo, first.queue, other_client, resources=resources,
        control_principal=first.principal, actions=first.actions, poll_interval=0.001)
    client.job = blocked_read
    pending = asyncio.create_task(first.reconcile_for('human:1', identity, admission_guard=lambda db: None))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        winner = await other.reconcile_for('human:1', identity, admission_guard=lambda db: None)
        assert winner['job']['state'] == 'succeeded'
        committed = first._read(job_id=identity)
        resume.set()
        with pytest.raises(HubError, match='staging_reconciliation_changed'):
            await pending
        assert first._read(job_id=identity) == committed
        assert first.queue.get_job(identity)['state'] == 'succeeded'
        assert client.posts == 1 and other_client.posts == 0
    finally:
        resume.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_reconcile_admission_rolls_back_if_session_guard_rejects(env):
    controller, client, _, _, repo = env
    client.lost_ack = True
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    def revoked(db):
        raise HubError('human_session_required', 401)
    with pytest.raises(HubError, match='human_session_required'):
        await controller.reconcile_for('human:1', submitted['job']['id'], admission_guard=revoked)
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_staging_reconcile_admissions').fetchone()[0] == 0
    assert controller.queue.get_job(submitted['job']['id'])['state'] == 'held'
    assert client.posts == 1


@pytest.mark.asyncio
async def test_reconcile_rogue_job_resource_during_worker_read_fails_closed(env):
    controller, client, _, _, repo = env
    client.lost_ack = True
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    before = controller._read(job_id=submitted['job']['id'])
    original = client.job
    rogue_id = str(uuid.uuid4())

    async def insert_during_read(identity):
        with repo._transaction() as db:
            db.execute('INSERT INTO hub_entities(id,kind,data) VALUES(?,?,?)',
                       (rogue_id, 'staging_resource', json.dumps({'id': rogue_id, 'job_id': identity})))
        return await original(identity)

    client.job = insert_during_read
    with pytest.raises(HubError, match='staging_reconciliation_changed'):
        await controller.reconcile_for('human:1', submitted['job']['id'], admission_guard=lambda db: None)
    assert controller._read(job_id=submitted['job']['id']) == before
    assert controller.queue.get_job(submitted['job']['id'])['state'] == 'held'
    assert client.posts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('drift', ['missing_kind', 'swapped_kinds', 'owner_mismatch'])
async def test_reconcile_inconsistent_resource_binding_cannot_release(env, drift):
    controller, client, _, _, repo = env
    client.lost_ack = True
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    identity = submitted['job']['id']
    item = controller._read(job_id=identity)
    kinds = sorted(item['resource_ids'])
    with repo._transaction() as db:
        if drift == 'missing_kind':
            item['resource_ids'].pop(kinds[0])
        elif drift == 'swapped_kinds':
            item['resource_ids'][kinds[0]], item['resource_ids'][kinds[1]] = item['resource_ids'][kinds[1]], item['resource_ids'][kinds[0]]
        else:
            db.execute('UPDATE hub_staging_resource_ownership SET project=? WHERE id=?',
                       ('other-project', item['resource_ids'][kinds[0]]))
        db.execute('UPDATE hub_staging_submissions SET data=? WHERE id=?', (json.dumps(item), item['id']))
    with pytest.raises(HubError, match='staging_reconciliation_changed'):
        await controller.reconcile_for('human:1', identity, admission_guard=lambda db: None)
    assert controller.queue.get_job(identity)['state'] == 'held'
    assert client.posts == 1


@pytest.mark.asyncio
async def test_unbound_partial_queued_submission_remains_held_without_release(env):
    controller, client, _, _, repo = env
    submitted = await controller.submit('codex', payload(repo))
    identity = submitted['job']['id']
    item = controller._read(job_id=identity)
    item['source_binding'] = None
    item['resource_ids'].pop(sorted(item['resource_ids'])[0])
    controller._save(item)
    result = await controller.reconcile_for('human:1', identity, admission_guard=lambda db: None)
    assert result['phase'] == 'held'
    assert result['job']['state'] == 'queued'
    assert client.posts == 0


@pytest.mark.asyncio
async def test_unrelated_malformed_resource_blocks_unbound_queued_recovery(env):
    controller, client, _, _, repo = env
    submitted = await controller.submit('codex', payload(repo))
    identity = submitted['job']['id']
    item = controller._read(job_id=identity)
    item['source_binding'] = None
    controller._save(item)
    corrupt_id = str(uuid.uuid4())
    with repo._transaction() as db:
        db.execute('INSERT INTO hub_entities(id,kind,data) VALUES(?,?,?)', (corrupt_id, 'staging_resource', '{'))
    with pytest.raises(HubError, match='staging_reconciliation_changed'):
        await controller.reconcile_for('human:1', identity, admission_guard=lambda db: None)
    with repo._connection() as db:
        assert db.execute('SELECT data FROM hub_entities WHERE id=?', (corrupt_id,)).fetchone()[0] == '{'
        assert db.execute('SELECT state FROM hub_work_jobs WHERE id=?', (identity,)).fetchone()[0] == 'queued'
    assert client.posts == 0


@pytest.mark.asyncio
async def test_malformed_legacy_resource_remains_intact_and_blocks_reconcile(env):
    controller, client, _, _, repo = env
    client.lost_ack = True
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    corrupt_id = str(uuid.uuid4())
    with repo._transaction() as db:
        db.execute('INSERT INTO hub_entities(id,kind,data) VALUES(?,?,?)', (corrupt_id, 'staging_resource', '{'))
        db.execute('DROP INDEX hub_staging_resource_job_ref')
        db.execute('DROP INDEX hub_staging_resource_bad_job_ref')
    # Initializing the indexes tolerates malformed retained rows; reconciliation
    # must fail closed, without deleting or adopting them.
    StagingResourceStore(repo, admin_actors=frozenset({'human:1'}), control_principal=controller.principal)
    with pytest.raises(HubError, match='staging_reconciliation_changed'):
        await controller.reconcile_for('human:1', submitted['job']['id'], admission_guard=lambda db: None)
    with repo._connection() as db:
        assert db.execute('SELECT data FROM hub_entities WHERE id=?', (corrupt_id,)).fetchone()[0] == '{'
    assert controller.queue.get_job(submitted['job']['id'])['state'] == 'held'
    assert client.posts == 1


def test_reconciliation_indexes_reject_collision_and_use_selective_queries(env):
    controller, _, _, _, repo = env
    with repo._transaction() as db:
        for n in range(1000):
            db.execute('INSERT INTO hub_entities(id,kind,data) VALUES(?,?,?)',
                       (f'legacy-resource-{n}', 'staging_resource', json.dumps({'job_id': f'legacy-job-{n}'})))
        queries = (
            ("SELECT id FROM hub_entities WHERE kind='staging_resource' AND (CASE WHEN json_valid(data) THEN json_extract(data,'$.job_id') END)=? ORDER BY id LIMIT ?", ('legacy-job-3', 5), 'hub_staging_resource_job_ref'),
            ("SELECT id FROM hub_entities WHERE kind='staging_resource' AND (CASE WHEN json_valid(data) THEN CASE WHEN json_type(data,'$.job_id')='text' THEN 0 ELSE 1 END ELSE 1 END)=1 LIMIT 1", (), 'hub_staging_resource_bad_job_ref'),
            ('SELECT * FROM hub_staging_resource_ownership WHERE job_id=? ORDER BY id LIMIT ?', ('legacy-job-3', 5), 'hub_staging_resource_ownership_job'),
            ("SELECT data FROM hub_entities WHERE kind='staging_resource' AND (CASE WHEN json_valid(data) THEN json_extract(data,'$.job_id') END)=? ORDER BY id LIMIT ?", ('legacy-job-3', 5), 'hub_staging_resource_job_ref'),
        )
        for query, args, expected in queries:
            plan = ' '.join(row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + query, args))
            assert expected in plan and 'SEARCH' in plan
        db.execute('DROP INDEX hub_staging_resource_job_ref')
        db.execute('CREATE INDEX hub_staging_resource_job_ref ON hub_entities(kind)')
    with pytest.raises(HubError, match='staging_resource_index_invalid'):
        StagingResourceStore(repo, admin_actors=frozenset({'human:1'}), control_principal=controller.principal)
    with repo._connection() as db:
        assert db.execute("SELECT sql FROM sqlite_master WHERE name='hub_staging_resource_job_ref'").fetchone()[0] == 'CREATE INDEX hub_staging_resource_job_ref ON hub_entities(kind)'
        assert db.execute("SELECT count(*) FROM hub_entities WHERE id LIKE 'legacy-resource-%'").fetchone()[0] == 1000


@pytest.mark.asyncio
async def test_terminal_reconcile_retry_reuses_exact_admission_receipt(env):
    controller, client, _, _, repo = env
    client.lost_ack = True
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    await controller.reconcile_for('human:1', submitted['job']['id'], admission_guard=lambda db: None)
    await controller.reconcile_for('human:1', submitted['job']['id'], admission_guard=lambda db: None)
    with repo._connection() as db:
        before = db.execute('SELECT count(*) FROM hub_staging_reconcile_admissions').fetchone()[0]
    await controller.reconcile_for('human:1', submitted['job']['id'], admission_guard=lambda db: None)
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_staging_reconcile_admissions').fetchone()[0] == before
    assert client.posts == 1


@pytest.mark.asyncio
async def test_reconcile_receipt_capacity_fails_closed_without_deleting_history(env, monkeypatch):
    import engineering_hub.staging_controller as module
    controller, client, _, _, repo = env
    client.lost_ack = True
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    async def unavailable(identity):
        raise HubError('staging_connection_unavailable', 503)
    client.job = unavailable
    monkeypatch.setattr(module, '_MAX_RECONCILIATION_ADMISSIONS', 1)
    identity = submitted['job']['id']
    await controller.reconcile_for('human:1', identity, admission_guard=lambda db: None)
    # Identical retries do not consume an additional receipt.
    await controller.reconcile_for('human:1', identity, admission_guard=lambda db: None)
    with repo._connection() as db:
        before = [tuple(row) for row in db.execute('SELECT * FROM hub_staging_reconcile_admissions')]
    assert len(before) == 1
    repo.set_pause('human:1', True)  # A genuinely different snapshot cannot reuse it.
    with pytest.raises(HubError, match='staging_reconciliation_admission_limit'):
        await controller.reconcile_for('human:1', identity, admission_guard=lambda db: None)
    with repo._connection() as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM hub_staging_reconcile_admissions')] == before
    assert controller.queue.get_job(identity)['state'] == 'held'
    # Background recovery does not require a human admission, even at capacity.
    await controller.reconcile(identity)
    with repo._connection() as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM hub_staging_reconcile_admissions')] == before
    assert client.posts == 1


@pytest.mark.asyncio
async def test_reconcile_session_revocation_before_and_after_admission(env):
    from fastapi import HTTPException
    from engineering_hub.auth import SessionStore
    controller, client, _, _, repo = env
    client.lost_ack = True
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    identity = submitted['job']['id']
    sessions = SessionStore(repo.path, repository=repo)
    session, guard = human_cleanup_guard(repo, sessions)
    sessions.delete_session(session['cookie_token'])
    with pytest.raises(HTTPException):
        await controller.reconcile_for('human:1', identity, admission_guard=guard)
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_staging_reconcile_admissions').fetchone()[0] == 0
    session, guard = human_cleanup_guard(repo, sessions)
    original = client.job
    async def logout_after_admission(job_id):
        sessions.delete_session(session['cookie_token'])
        return await original(job_id)
    client.job = logout_after_admission
    await controller.reconcile_for('human:1', identity, admission_guard=guard)
    assert controller.queue.get_job(identity)['state'] == 'succeeded'
    assert client.posts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('stop_after', range(1, 9))
async def test_reconcile_interrupted_after_each_local_commit_recovers_without_repost(env, monkeypatch, stop_after):
    from contextlib import contextmanager
    controller, client, resources, _, repo = env
    client.lost_ack = True
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    identity = submitted['job']['id']
    original = repo._transaction
    commits = [0]
    class SimulatedCrash(BaseException):
        pass
    @contextmanager
    def interrupted(*args, **kwargs):
        with original(*args, **kwargs) as db:
            yield db
        commits[0] += 1
        if commits[0] == stop_after:
            raise SimulatedCrash()
    monkeypatch.setattr(repo, '_transaction', interrupted)
    with pytest.raises(SimulatedCrash):
        await controller.reconcile_for('human:1', identity, admission_guard=lambda db: None)
    monkeypatch.setattr(repo, '_transaction', original)
    with repo._connection() as db:
        state = db.execute('SELECT state,machine_id FROM hub_work_jobs WHERE id=?', (identity,)).fetchone()
        machine = db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=?', (state['machine_id'],)).fetchone()
        if state['state'] not in ('succeeded', 'failed'):
            assert machine['job_id'] == identity
    fresh = HubRepository(repo.path)
    principal = object()
    reopened_resources = StagingResourceStore(fresh, admin_actors=frozenset({'human:1'}),
        control_principal=principal, clock=resources.clock)
    reopened_queue, actions = compose_queue(fresh, None, {'engineering-hub': ['static'], 'landscripting': ['static']}, 'staging-pc')
    restarted = StagingController(fresh, reopened_queue, client, resources=reopened_resources,
        control_principal=principal, actions=actions, poll_interval=0.001)
    result = await restarted.reconcile_for('human:1', identity, admission_guard=lambda db: None)
    assert result['job']['state'] == 'succeeded'
    assert client.posts == 1


@pytest.mark.skipif(sys.platform != 'linux', reason='requires POSIX SIGKILL')
@pytest.mark.asyncio
@pytest.mark.parametrize('stop_after', (1, 4, 8))
async def test_reconcile_process_killed_after_commit_recovers_without_repost(env, tmp_path, stop_after):
    """Kill only a disposable child after a committed local transaction.

    The remote worker is an in-memory synthetic receipt copied to the child;
    this exercises SQLite process recovery, not a real worker or power loss.
    """
    controller, client, resources, _, repo = env
    client.lost_ack = True
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    identity = submitted['job']['id']
    state_file = tmp_path / 'worker.json'
    state_file.write_text(json.dumps({'jobs': client.jobs, 'facts': client.facts,
                                      'bindings': client.bindings}), encoding='utf-8')
    child = r'''
import asyncio, importlib.util, json, os, signal, sys
from contextlib import contextmanager
spec = importlib.util.spec_from_file_location('staging_fixture', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
state = json.load(open(sys.argv[3], encoding='utf-8'))
repo = module.HubRepository(sys.argv[2])
principal = object()
resources = module.StagingResourceStore(repo, admin_actors=frozenset({'human:1'}),
    control_principal=principal, clock=lambda: 1800000000)
queue, actions = module.compose_queue(repo, None, {'engineering-hub': ['static'],
    'landscripting': ['static']}, 'staging-pc')
client = module.Client()
client.jobs, client.facts, client.bindings = state['jobs'], state['facts'], state['bindings']
async def forbidden_submit(payload):
    with open(sys.argv[6], 'w', encoding='utf-8') as marker:
        marker.write('unexpected submit')
        marker.flush()
        os.fsync(marker.fileno())
    raise AssertionError('reconciliation must not submit')
client.submit = forbidden_submit
client.cleanup = forbidden_submit
client.put_source = forbidden_submit
controller = module.StagingController(repo, queue, client, resources=resources,
    control_principal=principal, actions=actions, poll_interval=0.001)
original = repo._transaction
commits = 0
@contextmanager
def kill_after_commit(*args, **kwargs):
    global commits
    with original(*args, **kwargs) as db:
        yield db
    commits += 1
    if commits == int(sys.argv[5]):
        with repo._connection() as check:
            job = check.execute('SELECT state, revision FROM hub_work_jobs WHERE id=?', (sys.argv[4],)).fetchone()
            submission = json.loads(check.execute('SELECT data FROM hub_staging_submissions').fetchone()[0])
            states = [json.loads(row[0]).get('state') for row in check.execute(
                "SELECT data FROM hub_entities WHERE kind='staging_resource'")]
            snapshot = {'commit': commits, 'job_state': job['state'], 'job_revision': job['revision'],
                        'submission_phase': submission['phase'], 'resource_states': sorted(states),
                        'admissions': check.execute('SELECT count(*) FROM hub_staging_reconcile_admissions').fetchone()[0]}
        with open(sys.argv[7], 'w', encoding='utf-8') as marker:
            json.dump(snapshot, marker)
            marker.flush()
            os.fsync(marker.fileno())
        os.kill(os.getpid(), signal.SIGKILL)
repo._transaction = kill_after_commit
asyncio.run(controller.reconcile_for('human:1', sys.argv[4], admission_guard=lambda db: None))
'''
    post_marker = tmp_path / 'unexpected-post'
    checkpoint = tmp_path / 'checkpoint.json'
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', child, __file__, str(repo.path),
                                                   str(state_file), identity, str(stop_after), str(post_marker), str(checkpoint),
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == -signal.SIGKILL, (process.returncode, stdout, stderr)
    assert not post_marker.exists()
    observed = json.loads(checkpoint.read_text(encoding='utf-8'))
    expected = {
        1: ('held', 'held', ['provisioning'] * 4),
        4: ('held', 'held', ['provisioning', 'ready', 'ready', 'ready']),
        8: ('succeeded', 'verified', ['ready'] * 4),
    }
    job_state, phase, resource_states = expected[stop_after]
    assert observed['commit'] == stop_after and observed['admissions'] == 1
    assert (observed['job_state'], observed['submission_phase'], observed['resource_states']) == (
        job_state, phase, resource_states)
    fresh = HubRepository(repo.path)
    with fresh._connection() as db:
        state = db.execute('SELECT state,machine_id FROM hub_work_jobs WHERE id=?', (identity,)).fetchone()
        machine = db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=?', (state['machine_id'],)).fetchone()
        if state['state'] not in ('succeeded', 'failed'):
            assert machine['job_id'] == identity
    principal = object()
    reopened_resources = StagingResourceStore(fresh, admin_actors=frozenset({'human:1'}),
        control_principal=principal, clock=resources.clock)
    reopened_queue, actions = compose_queue(fresh, None, {'engineering-hub': ['static'], 'landscripting': ['static']}, 'staging-pc')
    restarted = StagingController(fresh, reopened_queue, client, resources=reopened_resources,
        control_principal=principal, actions=actions, poll_interval=0.001)
    result = await restarted.reconcile_for('human:1', identity, admission_guard=lambda db: None)
    assert result['job']['state'] == 'succeeded'
    assert client.posts == 1


@pytest.mark.asyncio
async def test_restart_follows_held_original_receipt_without_post(env):
    repo = env[4]
    old, client, resources, _, _ = env
    client.lost_ack = True
    submitted = await old.submit('codex', payload(repo))
    await old.run_once()
    restarted = StagingController(old.repo, old.queue, client, resources=resources,
        control_principal=old.principal, actions=old.actions, poll_interval=0.001)
    assert restarted.max_poll_seconds >= 2 * 900 + 300
    await restarted.start()
    try:
        async with asyncio.timeout(2):
            while restarted.queue.get_job(submitted['job']['id'])['state'] != 'succeeded':
                await asyncio.sleep(0.01)
    finally:
        await restarted.close()
    assert client.posts == 1


@pytest.mark.asyncio
async def test_revocation_after_claim_before_dispatch_releases_only_proven_no_effect(env, monkeypatch):
    repo = env[4]
    controller, client, resources, _, _ = env
    submitted = await controller.submit('codex', payload(repo))
    original = controller.queue.claim
    def revoked_after_claim(**kwargs):
        claim = original(**kwargs)
        policy = resources.get_policy('human:1', 'engineering-hub')
        resources.set_policy('human:1', 'engineering-hub', policy['revision'], {'human:1': ['read']})
        return claim
    monkeypatch.setattr(controller.queue, 'claim', revoked_after_claim)
    result = await controller.run_once()
    assert result['job']['state'] == 'failed' and client.posts == 0
    assert all(resources.inspect_resource(controller.principal, identity)['state'] == 'deleted'
               for identity in submitted['resource_ids'].values())
    with controller.repo._connection() as db:
        assert db.execute('SELECT job_id FROM hub_work_machines').fetchone()[0] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('reason,expected', [('project_not_allowed', 'failed'), ('worker_busy_or_held', 'held')])
async def test_rejected_uncreated_command_distinct_from_unknown_busy_machine(env, reason, expected):
    repo = env[4]
    controller, client, _, _, _ = env
    async def rejected(request):
        client.posts += 1
        raise StagingNotAccepted(reason)
    client.submit = rejected
    await controller.submit('codex', payload(repo))
    result = await controller.run_once()
    assert result['job']['state'] == expected and client.posts == 1


@pytest.mark.asyncio
async def test_claim_crash_before_dispatch_is_proven_unstarted_after_expiry(env):
    repo = env[4]
    controller, client, _, _, _ = env
    now = [1000.0]
    controller.queue.clock = lambda: now[0]
    submitted = await controller.submit('codex', payload(repo))
    assert controller.queue.claim(worker=controller.worker, lease_seconds=10)
    # Simulate process death between the queue commit and dispatch-intent save.
    assert controller._read(job_id=submitted['job']['id'])['phase'] == 'queued'
    now[0] += 11
    result = await controller.reconcile_for('human:1', submitted['job']['id'], admission_guard=lambda db: None)
    assert result['job']['state'] == 'failed' and client.posts == 0
    with controller.repo._connection() as db:
        assert db.execute('SELECT job_id FROM hub_work_machines').fetchone()[0] is None


@pytest.mark.asyncio
async def test_unchanged_unknown_hold_does_not_grow_audit_history(env):
    repo = env[4]
    controller, client, _, _, _ = env
    client.lost_ack = True
    submitted = await controller.submit('codex', payload(repo))
    await controller.run_once()
    async def unavailable(identity):
        raise HubError('staging_connection_unavailable', 503)
    client.job = unavailable
    before = controller.repo.list_audit()
    await controller.reconcile_for('human:1', submitted['job']['id'], admission_guard=lambda db: None)
    await controller.reconcile_for('human:1', submitted['job']['id'], admission_guard=lambda db: None)
    assert controller.repo.list_audit() == before and client.posts == 1


@pytest.mark.asyncio
async def test_public_operations_report_busy_without_waiting_for_long_job(env):
    repo = env[4]
    controller, client, _, _, _ = env
    submitted = await controller.submit('codex', payload(repo))
    await controller.lock.acquire()
    try:
        async with asyncio.timeout(2):
            for operation in (controller.submit('codex', payload(repo)),
                              controller.cleanup_apply('human:1', str(uuid.uuid4()), 'sha256:' + 'a' * 64,
                                                       admission_guard=lambda db: None),
                              controller.reconcile_for('human:1', submitted['job']['id'], admission_guard=lambda db: None)):
                with pytest.raises(HubError, match='staging_busy'):
                    await operation
    finally:
        controller.lock.release()
    assert client.posts == 0 and client.deposits == 1


@pytest.mark.asyncio
async def test_failed_background_still_closes_private_client(env):
    repo = env[4]
    controller, client, _, _, _ = env
    closed = []
    async def failure():
        raise HubError('synthetic_background_failure', 503)
    async def close():
        closed.append(True)
    client.aclose = close
    controller.background = asyncio.create_task(failure())
    with pytest.raises(HubError, match='synthetic_background_failure'):
        await controller.close()
    assert closed == [True]


@pytest.mark.parametrize('worker_blocked', [False, True], ids=['completes', 'timeout-holds'])
@pytest.mark.asyncio
async def test_actual_client_contract_through_registered_worker_core(env, tmp_path, worker_blocked):
    repo = env[4]
    import httpx
    import threading
    from engineering_hub.staging_client import StagingClient, StagingConnectionConfig
    from hub_staging_service.core import Service
    profile = {'image_id': 'sha256:' + 'a' * 64, 'build_argv': ['builder'], 'test_argv': ['tests'],
               'timeout_seconds': 30, 'memory_mb': 128, 'cpus': 1, 'pids': 32,
               'output_bytes': 1048576, 'log_bytes': 65536}
    release_worker = threading.Event()
    entered_worker = threading.Event()
    backend_calls = []
    class Backend:
        def run(self, paths, profile, command_id):
            backend_calls.append(command_id)
            entered_worker.set()
            if worker_blocked and not release_worker.wait(5):
                raise RuntimeError('test worker release timed out')
            for kind in ('build', 'preview'):
                (paths[kind] / 'index.html').write_bytes(b'<html>real-contract</html>')
            return {'preview_ready': True, 'stages': {stage: {'exit_code': 0, 'log_bytes': 0} for stage in ('build', 'test')}}
    service = Service(tmp_path / 'private-worker', {'engineering-hub': {'recipes': {'static': profile}}}, Backend())
    def transport(request):
        path = request.url.path.split('/')
        if request.method == 'POST':
            import json
            body = json.loads(request.content)
            result = service.put_source(body) if path[-1] == 'sources' else service.submit(body)
            if worker_blocked and path[-1] == 'jobs':
                assert entered_worker.wait(2), 'worker did not enter backend before polling'
            return httpx.Response(202 if path[-1] == 'jobs' else 200, json=result)
        if path[2] == 'jobs':
            return httpx.Response(200, json=service.status(path[3]))
        if path[-1] == 'metadata':
            return httpx.Response(200, json=service.metadata(path[3]))
        if path[4] == 'files':
            return httpx.Response(200, content=service.file(path[3], '/'.join(path[5:])), headers={'Content-Type': 'text/html'})
        raise AssertionError('unexpected request')
    token = tmp_path / 'synthetic-token'
    token.write_text('synthetic-test-worker-secret-only'); token.chmod(0o600)
    client = StagingClient(StagingConnectionConfig('http://127.0.0.1:8780', token, enabled=True,
                           projects={'engineering-hub': {'recipes': ['static']}}), test_transport=httpx.MockTransport(transport))
    controller = env[0]
    controller.client = client
    # The shared fixture's 100ms window tests fast fakes; the actual worker
    # has a thread and filesystem work that may be delayed under a CPU limit.
    controller.max_poll_seconds = 0.1 if worker_blocked else 5
    try:
        submitted = await controller.submit('codex', payload(repo))
        finished = await controller.run_once()
        receipt = service.status(submitted['job']['id'])
        expected = 'held' if worker_blocked else 'succeeded'
        assert finished['job']['state'] == expected, {'controller': finished, 'worker_receipt': receipt}
        if worker_blocked:
            assert entered_worker.is_set()
            assert receipt['state'] == 'running', receipt
            release_worker.set()
            service.wait()
            assert service.status(submitted['job']['id'])['state'] == 'succeeded'
            assert len(backend_calls) == 1
        else:
            assert await controller.preview_file(submitted['resource_ids']['preview'], 'index.html') == (b'<html>real-contract</html>', 'text/html')
    finally:
        release_worker.set()
        try:
            await client.aclose()
        finally:
            service.close()

@pytest.mark.asyncio
async def test_cancel_during_submit_resource_registration_never_abandons_deposit(env):
    controller, client, resources, _, repo = env
    original=controller.queue.submit
    observed=[]
    def interrupted(*args, **kwargs):
        job=original(*args, **kwargs)
        with repo._connection() as db:
            row=db.execute('SELECT data FROM hub_staging_submissions').fetchone()
            assert json.loads(row['data'])['job_id'] is None
        with pytest.raises(HubError, match='work_controller_cancel_required'):
            controller.queue.cancel_unclaimed(actor='codex',job_id=job['id'])
        observed.append(job['id'])
        return job
    controller.queue.submit=interrupted
    submitted=await controller.submit('codex',payload(repo))
    assert observed==[submitted['job']['id']]
    assert submitted['job']['state']=='queued'
    assert submitted['source_id'] and len(submitted['resource_ids'])==4
    assert client.deposits==1 and client.posts==0
    assert (await controller.run_once())['job']['state']=='succeeded'

@pytest.mark.asyncio
async def test_late_old_failure_reconciliation_never_unprotects_newer_failure(env):
    controller, client, resources, _, repo = env
    client.fail = True
    first = await controller.submit('codex', payload(repo))
    assert (await controller.run_once())['job']['state'] == 'failed'
    second = await controller.submit('codex', payload(repo))
    assert (await controller.run_once())['job']['state'] == 'failed'
    newest_id = second['resource_ids']['test_run']
    newest = resources.inspect_resource(controller.principal, newest_id)
    assert {'reason': 'latest_failure', 'ref': second['job']['id']} in newest['protected']
    # The first controller may be delayed after it released the queue, while a
    # second controller completes a newer failure. Resume the stale callback.
    controller._supersede_failures(controller._read(job_id=first['job']['id']))
    assert resources.inspect_resource(controller.principal, newest_id) == newest

@pytest.mark.asyncio
async def test_two_controller_late_failure_callback_preserves_newest_protection(env):
    import threading
    controller, client, resources, _, repo = env
    client.fail = True
    first = await controller.submit('codex', payload(repo))
    second = await controller.submit('codex', payload(repo))
    waiting, resume = threading.Event(), threading.Event()
    original = controller._supersede_failures

    def delayed(item):
        if item['job_id'] == first['job']['id']:
            waiting.set()
            if not resume.wait(15):
                raise RuntimeError('test callback not released')
        return original(item)

    controller._supersede_failures = delayed
    first_task = asyncio.create_task(asyncio.to_thread(lambda: asyncio.run(controller.run_once())))
    try:
        assert await asyncio.to_thread(waiting.wait, 10), 'first did not release before callback'
        assert controller.queue.get_job(first['job']['id'])['state'] == 'failed'
        second_controller = StagingController(repo, controller.queue, client, resources=resources,
            control_principal=controller.principal, actions=controller.actions,
            poll_interval=0.001, max_poll_seconds=0.1)
        result = await second_controller.run_once()
        assert result['job']['id'] == second['job']['id'] and result['job']['state'] == 'failed'
        newest_id = second['resource_ids']['test_run']
        newest = resources.inspect_resource(controller.principal, newest_id)
        assert {'reason': 'latest_failure', 'ref': second['job']['id']} in newest['protected']
    finally:
        resume.set()
        await first_task
    assert resources.inspect_resource(controller.principal, newest_id) == newest

@pytest.mark.asyncio
@pytest.mark.parametrize('uncertain', ['missing_event', 'duplicate_event', 'unprotected_newer', 'wrong_binding'])
async def test_failure_supersession_retains_protection_when_order_evidence_uncertain(env, uncertain):
    controller, client, resources, _, repo = env
    client.fail = True
    first = await controller.submit('codex', payload(repo))
    assert (await controller.run_once())['job']['state'] == 'failed'
    second = await controller.submit('codex', payload(repo))
    assert (await controller.run_once())['job']['state'] == 'failed'
    old_id, new_job_id = first['resource_ids']['test_run'], second['job']['id']
    old = resources.inspect_resource(controller.principal, old_id)
    marker = {'reason': 'latest_failure', 'ref': first['job']['id']}
    old = resources.set_protection(controller.principal, old_id, old['revision'], old['protected'] + [marker])
    with repo._transaction() as db:
        if uncertain == 'missing_event':
            db.execute("DELETE FROM hub_audit WHERE entity_id=? AND action IN ('work.failed','work.reconciled')", (new_job_id,))
        elif uncertain == 'duplicate_event':
            db.execute("INSERT INTO hub_audit(actor,action,entity_id,revision,created_at) SELECT actor,action,entity_id,revision,created_at FROM hub_audit WHERE entity_id=? AND action IN ('work.failed','work.reconciled') LIMIT 1", (new_job_id,))
        elif uncertain == 'unprotected_newer':
            row = db.execute('SELECT data FROM hub_staging_resource_jobs WHERE id=?', (new_job_id,)).fetchone()
            data = json.loads(row['data']); data['protected'] = []
            db.execute('UPDATE hub_staging_resource_jobs SET data=? WHERE id=?', (json.dumps(data), new_job_id))
        else:
            row = db.execute('SELECT target FROM hub_work_jobs WHERE id=?', (new_job_id,)).fetchone()
            target = json.loads(row['target']); target['submission_id'] = str(uuid.uuid4())
            db.execute('UPDATE hub_work_jobs SET target=? WHERE id=?', (json.dumps(target), new_job_id))
    controller._supersede_failures(controller._read(job_id=new_job_id))
    assert resources.inspect_resource(controller.principal, old_id) == old

@pytest.mark.asyncio
async def test_failure_supersession_error_never_rewrites_terminal_job_as_held(env, monkeypatch, caplog):
    controller, client, resources, _, repo = env
    client.fail = True
    submitted = await controller.submit('codex', payload(repo))
    def broken(item):
        raise RuntimeError('synthetic maintenance failure')
    monkeypatch.setattr(controller, '_supersede_failures', broken)
    result = await controller.run_once()
    assert result['job']['state'] == 'failed' and result['phase'] == 'verified'
    assert controller.queue.get_job(submitted['job']['id'])['state'] == 'failed'
    resource = resources.inspect_resource(controller.principal, submitted['resource_ids']['test_run'])
    assert {'reason': 'latest_failure', 'ref': submitted['job']['id']} in resource['protected']
    assert 'staging_failure_retention_maintenance_deferred' in caplog.text


def test_failure_terminal_audit_lookup_uses_index(env):
    repo = env[4]
    with repo._connection() as db:
        detail = db.execute("EXPLAIN QUERY PLAN SELECT sequence FROM hub_audit WHERE entity_id=? AND revision=? AND action IN ('work.failed','work.reconciled') ORDER BY sequence LIMIT 2", (str(uuid.uuid4()), 1)).fetchall()
    assert any('hub_audit_terminal_lookup' in row['detail'] for row in detail)
