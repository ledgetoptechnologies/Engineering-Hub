"""Short-lived actual-HTTP coordination smoke; no host effects or real tokens.

Run inside the isolated validation container. Production approval below is a
synthetic callback for priority testing, NOT authorization for any production
deployment. Both actions are explicitly coordination-only. This does not prove
the executor, NAS ingress, Cloudflare sign-in, or reboot recovery.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import secrets
import socket
import sys
import tempfile
from uuid import uuid4

import httpx
import uvicorn

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.repository import AGENTS, HubRepository
from engineering_hub.work_queue import WorkAction, WorkQueue


BASE = '/api/hub/v1/work'
COMMIT = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64


def check(condition, code):
    if not condition:
        raise RuntimeError(code)


async def _pointer(lines):
    async for line in lines:
        if line.startswith('data: '):
            pointer = json.loads(line[6:])
            check(set(pointer) == {'observer_epoch', 'cursor', 'kind'}, 'unexpected_event_fields')
            check(pointer['kind'] == 'invalidate', 'event_source_unavailable')
            return pointer
    raise RuntimeError('event_stream_closed')


async def exercise(root):
    repo = HubRepository(root / 'hub.sqlite3')
    tokens = {agent: secrets.token_urlsafe(32) for agent in AGENTS}
    auth = AuthSettings(tokens['hermes'], tokens['codex'], claude_token=tokens['claude'])
    now = [1000.0]
    actions = {'synthetic.check': WorkAction('synthetic-machine',
                frozenset({'staging', 'production'}), project='engineering-hub',
                requires_execution_proof=False)}

    def target_verifier(actor, action, environment, target):
        task = repo.get_task(target.task_id)
        review = task.get('review', {})
        return (task['owner'] == actor and task['project'] == actions[action].project
                and task['status'] == 'verified' and task['candidate_commit'] == target.source_commit
                and review.get('verdict') == 'approve' and review.get('actor') != actor
                and review.get('actor') in AGENTS and review.get('commit') == target.source_commit)

    queue = WorkQueue(repo, actions, clock=lambda: now[0], target_verifier=target_verifier,
                      approval_verifier=lambda actor, action, env, approval, target:
                          approval == 'synthetic-approval-only' and env == 'production')
    settings = Settings(root / 'hub.sqlite3', auth, AuthConfig('https://synthetic.invalid'),
                        ingress_mode='agents')
    app = create_app(settings, repository=repo, work_queue=queue)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(16)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                              log_level='critical', access_log=False, proxy_headers=False,
                              timeout_graceful_shutdown=3, lifespan='on'))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                    raise RuntimeError('server_not_started')
                await asyncio.sleep(0.01)  # Bounded process-start handshake only.
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', trust_env=False,
                                      timeout=httpx.Timeout(5)) as client:
            headers = {agent: {'Authorization': 'Bearer ' + token} for agent, token in tokens.items()}

            async def request(method, path, *, actor='codex', payload=None, expected=200):
                response = await client.request(method, BASE + path, headers=headers[actor], json=payload)
                check(response.status_code == expected, 'http_contract_' + str(response.status_code))
                return response.json()

            def submission(environment):
                task = repo.create_task('human:42', 'engineering-hub', 'Synthetic queue smoke', str(uuid4()))
                task = repo.claim_task('codex', task['id'], task['revision'])
                task = repo.set_candidate('codex', task['id'], task['revision'], COMMIT)
                task = repo.review_task('claude', task['id'], task['revision'], COMMIT, 'approve')
                return {'request_id': str(uuid4()), 'action_key': 'synthetic.check',
                        'environment': environment,
                        'target': {'task_id': task['id'], 'source_commit': COMMIT, 'artifact_digest': DIGEST},
                        'approval_id': 'synthetic-approval-only' if environment == 'production' else None}

            denied = await client.get(BASE + '/status', headers={'Authorization': 'Bearer synthetic-invalid'})
            check(denied.status_code == 401, 'bad_token_not_denied')
            for agent in AGENTS:
                await request('GET', '/status', actor=agent)

            feed = await client.get('/api/hub/v1/events/status', headers=headers['codex'])
            check(feed.status_code == 200, 'event_status_failed')
            native = feed.json()['available']
            check(native or sys.platform != 'linux', 'linux_native_feed_required')
            stage_payload = submission('staging')
            if native:
                async with client.stream('GET', '/api/hub/v1/events', headers=headers['codex']) as response:
                    check(response.status_code == 200, 'sse_connect_failed')
                    lines = response.aiter_lines()
                    initial = await asyncio.wait_for(_pointer(lines), 5)
                    stage = await request('POST', '/jobs', payload=stage_payload, expected=201)
                    with repo._connection() as db:
                        queue_head = db.execute('SELECT MAX(sequence) FROM hub_outbox').fetchone()[0]
                        check(db.execute('SELECT action FROM hub_outbox ORDER BY sequence DESC LIMIT 1').fetchone()[0] == 'work.submitted', 'queue_not_latest_event')
                    async with asyncio.timeout(5):
                        changed = await _pointer(lines)
                        while changed['cursor'] < queue_head:
                            changed = await _pointer(lines)
                    check(changed['cursor'] >= queue_head > initial['cursor'], 'queue_event_missing')
                    check(changed['observer_epoch'] == initial['observer_epoch'], 'event_epoch_changed')
            else:
                stage = await request('POST', '/jobs', payload=stage_payload, expected=201)
            production = await request('POST', '/jobs', payload=submission('production'), expected=201)
            instance = str(uuid4())
            claim = (await request('POST', '/claim', actor='hermes', payload={'instance_id': instance}))['job']
            check(claim['id'] == production['id'], 'production_priority_failed')
            another_production = await request('POST', '/jobs', payload=submission('production'), expected=201)
            check((await request('POST', '/claim', actor='claude', payload={'instance_id': str(uuid4())}))['job'] is None,
                  'running_job_preempted_or_machine_overlapped')
            receipt = {'instance_id': instance, 'fence': claim['fence']}
            await request('POST', f"/jobs/{claim['id']}/renew", actor='hermes',
                          payload={**receipt, 'instance_id': str(uuid4())}, expected=409)
            await request('POST', f"/jobs/{claim['id']}/complete", actor='hermes',
                          payload={**receipt, 'outcome': 'succeeded'})
            claim2 = (await request('POST', '/claim', actor='hermes', payload={'instance_id': instance}))['job']
            check(claim2['id'] == another_production['id'], 'queued_production_priority_failed')
            await request('POST', f"/jobs/{claim2['id']}/complete", actor='hermes',
                          payload={'instance_id': instance, 'fence': claim2['fence'], 'outcome': 'succeeded'})
            claim3 = (await request('POST', '/claim', actor='hermes',
                                   payload={'instance_id': instance, 'lease_seconds': 1}))['job']
            check(claim3['id'] == stage['id'], 'staging_not_eventually_claimed')
            now[0] += 1
            check((await request('POST', '/claim', actor='claude', payload={'instance_id': str(uuid4())}))['job'] is None,
                  'expired_work_replayed')
            status = await request('GET', '/status')
            held = next(row for row in status['jobs'] if row['id'] == stage['id'])
            check(held['state'] == 'held' and held['requires_reconciliation'], 'expired_machine_not_held')
            check(repo.list_runs() == [], 'coordination_created_deployment')
            check(repo.get_task(stage_payload['target']['task_id'])['status'] == 'verified', 'coordination_promoted_task')
            return {'coordination_only': True, 'host_effects_exercised': False,
                    'actual_http': True, 'synthetic_production_approval': True,
                    'production_priority': True, 'non_preemptive_machine_exclusion': True,
                    'instance_fencing': True, 'expired_claim_held': True,
                    'bad_token_401': True, 'native_sse_queue_invalidation': native,
                    'deployment_runs_created': 0, 'nas_or_reboot_acceptance': False}
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(serving), 6)
        except asyncio.TimeoutError:
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
            raise RuntimeError('server_shutdown_timeout') from None
        finally:
            listener.close()
            repo.close()


def main():
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix='hub-work-queue-smoke-') as root:
        result = asyncio.run(asyncio.wait_for(exercise(Path(root)), 40))
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # No exception payload, request headers, tokens, or temp paths in output.
        print(json.dumps({'coordination_only': True, 'host_effects_exercised': False,
                          'smoke_failed': True}), file=sys.stderr)
        raise SystemExit(1) from None
