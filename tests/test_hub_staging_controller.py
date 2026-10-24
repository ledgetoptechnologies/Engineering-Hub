import base64
import asyncio
import hashlib
import io
import uuid
import zipfile

import pytest

from engineering_hub.projects import ProjectRepository
from engineering_hub.repository import HubRepository, HubError
from engineering_hub.staging_resources import StagingResourceStore
from engineering_hub.staging_controller import StagingController, compose_queue
from engineering_hub.work_queue import WorkAction, WorkQueue, WorkTarget


class Client:
    def __init__(self):
        self.jobs, self.facts, self.posts, self.deposits = {}, {}, 0, 0
        self.lost_ack, self.cleanup_lost = False, False
        self.cleanup_receipts, self.cleanup_posts = {}, 0
        self.fail = False
        self.bytes = b'<html>demo</html>'

    async def status(self):
        return {'projects': {'engineering-hub': ['static'], 'landscripting': ['static']}}

    async def put_source(self, payload):
        self.deposits += 1
        return {key: payload[key] for key in ('source_id', 'project', 'source_digest')} | {'size_bytes': 1}

    async def submit(self, payload):
        self.posts += 1
        result = {key: payload[key] for key in ('source_commit', 'source_digest', 'resource_ids')}
        result.update(source_commit_verification='declared', preview_ready=True,
                      stages={stage: {'exit_code': 0, 'log_bytes': 0} for stage in ('build', 'test')})
        receipt = {'job_id': payload['job_id'], 'command_id': payload['command_id'], 'state': 'succeeded', 'result': result}
        self.jobs[payload['job_id']] = receipt
        if self.fail:
            receipt.update(state='failed', result={'error': 'stage_test_failed'})
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


def payload(project='engineering-hub'):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as bundle:
        bundle.writestr('index.html', '<html>source</html>')
    raw = stream.getvalue()
    return {'request_id': str(uuid.uuid4()), 'project': project, 'recipe': 'static',
            'source_commit': 'a' * 40, 'source_digest': hashlib.sha256(raw).hexdigest(),
            'source_zip_base64': base64.b64encode(raw).decode()}


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
async def test_deposit_queue_build_proof_and_preview_bytes(env):
    controller, client, resources, _, _ = env
    submitted = await controller.submit('codex', payload('landscripting'))
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
    controller, client, _, _, repo = env
    request = payload()
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
    controller, client, resources, _, _ = env
    submitted = await controller.submit('codex', payload())
    resources.set_policy('human:1', 'engineering-hub', 1, {'codex': ['read', 'build']})
    assert await controller.run_once() is None
    assert client.posts == 0
    assert controller.queue.get_job(submitted['job']['id'])['state'] == 'queued'


@pytest.mark.asyncio
async def test_lost_acceptance_holds_then_original_get_reconciles_without_repost(env):
    controller, client, _, _, _ = env
    submitted = await controller.submit('codex', payload())
    client.lost_ack = True
    held = await controller.run_once()
    assert held['job']['state'] == 'held'
    assert client.posts == 1
    finished = await controller.reconcile(submitted['job']['id'])
    assert finished['job']['state'] == 'succeeded'
    assert client.posts == 1


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
    await controller.submit('codex', payload())
    assert await controller.run_once() is None
    assert queue.get_job(production['id'])['state'] == 'queued'
    assert queue.target_verifier('codex', 'production', 'production', target) is True


@pytest.mark.asyncio
async def test_cleanup_exact_receipt_tombstone_and_uncertainty_never_replayed(env):
    controller, client, resources, now, _ = env
    submitted = await controller.submit('codex', payload())
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


@pytest.mark.asyncio
async def test_humans_cannot_impersonate_queue_agent(env):
    with pytest.raises(HubError, match='staging_agent_required'):
        await env[0].submit('human:1', payload())


@pytest.mark.asyncio
async def test_explicit_start_close_without_worker_effects(env):
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
    await controller.submit('codex', payload())
    assert await controller.run_once() is None
    assert client.posts == 0


@pytest.mark.asyncio
async def test_new_failure_supersedes_only_old_latest_failure_protection(env):
    controller, client, resources, now, _ = env
    client.fail = True
    first = await controller.submit('codex', payload())
    assert (await controller.run_once())['job']['state'] == 'failed'
    old = resources.inspect_resource(controller.principal, first['resource_ids']['preview'])
    resources.set_protection(controller.principal, old['id'], old['revision'], old['protected'] +
                              [{'reason': 'decision_evidence', 'ref': 'retain-this'}])
    second = await controller.submit('codex', payload())
    assert (await controller.run_once())['job']['state'] == 'failed'
    old = resources.get_resource('codex', first['resource_ids']['preview'])
    assert {'reason': 'decision_evidence', 'ref': 'retain-this'} in old['protected']
    assert not any(p['reason'] == 'latest_failure' for p in old['protected'])
    now[0] += 32 * 86400
    plan = resources.cleanup_plan('codex', 'engineering-hub', kinds=['test_run'])
    assert first['resource_ids']['test_run'] in {entry['id'] for entry in plan['remove']}
    assert second['resource_ids']['test_run'] not in {entry['id'] for entry in plan['remove']}


@pytest.mark.asyncio
async def test_caller_cancellation_does_not_cancel_owned_dispatch_and_close_drains(env):
    controller, client, _, _, _ = env
    entered, release = asyncio.Event(), asyncio.Event()
    original = client.submit
    async def blocked(request):
        entered.set()
        await release.wait()
        return await original(request)
    client.submit = blocked
    submitted = await controller.submit('codex', payload())
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


@pytest.mark.asyncio
async def test_actual_client_contract_through_registered_worker_core(env, tmp_path):
    import httpx
    from engineering_hub.staging_client import StagingClient, StagingConnectionConfig
    from hub_staging_service.core import Service
    profile = {'image_id': 'sha256:' + 'a' * 64, 'build_argv': ['builder'], 'test_argv': ['tests'],
               'timeout_seconds': 30, 'memory_mb': 128, 'cpus': 1, 'pids': 32,
               'output_bytes': 1048576, 'log_bytes': 65536}
    class Backend:
        def run(self, paths, profile, command_id):
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
    try:
        submitted = await controller.submit('codex', payload())
        finished = await controller.run_once()
        assert finished['job']['state'] == 'succeeded'
        assert await controller.preview_file(submitted['resource_ids']['preview'], 'index.html') == (b'<html>real-contract</html>', 'text/html')
    finally:
        await client.aclose()
        service.close()
