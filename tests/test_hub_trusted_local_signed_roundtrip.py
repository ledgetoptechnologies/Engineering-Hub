"""Synthetic Linux signed supervisor -> private UDS -> authenticated human gate.

Real signatures, HTTP framing, local reconciliation and audit are exercised;
the reused supervisor fixture explicitly does not prove a real Engine launch.
"""
import asyncio
from dataclasses import replace
import hashlib
import json
import os
import socket
import threading
import time

import pytest
import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
pytest.importorskip('fcntl')

from agent_message_bus.auth import AuthSettings
from engineering_hub import cloudflare_access as access
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.events import NativeHubEventFeed
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.queued_pilot import QueuedContainerPilot
from engineering_hub.queued_pilot_runtime import QueuedPilotRuntime
from engineering_hub.repository import HubRepository
from engineering_hub.trusted_queued_session import parse_enrollment
from engineering_hub.work_queue import WorkAction, WorkQueue
from hub_container_executor.asgi import create_app as executor_app
from hub_container_executor.installed_coordinator import parse_configuration
from hub_container_executor.launch_transport import UnixCurrentObservationTransport
from hub_container_executor.trusted_local_session import TrustedLocalQueuedSession
from tests.test_hub_container_executor_installed_coordinator import configuration, canonical
from tests.test_hub_container_executor_launch_supervisor import synthetic_issuer, short_root, root_journal, current_pins, current_ready
from tests.test_hub_current_observation_end_to_end import owned_supervisor_wait, request, NoCommands
from tests.test_hub_trusted_queued_session import v2_document, encode
from tests.test_hub_unified_pilot_startup import profile_document
from tests.test_hub_queued_pilot import DistinctContainerClient, ACTION, MACHINE


@pytest.mark.asyncio
@pytest.mark.parametrize('rollback', [False, True])
async def test_local_signed_observation_requires_authenticated_operator(synthetic_issuer, monkeypatch, rollback):
    import hub_container_executor.trusted_local_session as installed_local
    if os.environ.get('TEST_INSTALLED_EXECUTOR'):
        assert installed_local.__file__.startswith('/tmp/installed-executor/')
    import engineering_hub.pilot_ownership as ownership
    observed_filesystem = ownership._local_filesystem
    monkeypatch.setattr(ownership, '_local_filesystem', lambda fd, **_: observed_filesystem(fd))
    authority, issuer_path, checks = synthetic_issuer
    data = configuration(); data['manifest'] = profile_document()['manifest']
    from engineering_hub.container_pilot_manifest import PilotManifest
    manifest = PilotManifest.from_bytes(canonical(data['manifest']))
    authority.release = replace(authority.release, implementation_commit=manifest.driver.implementation_commit,
                                platform=manifest.runner_binding.platform)
    authority._daemon_pin = replace(authority._daemon_pin, daemon_id=manifest.runner_binding.daemon_identity)
    current_ready(authority)
    release, launch = current_pins(authority)
    evidence = v2_document()
    from engineering_hub.executor_launch_profile import canonical as profile_canonical
    from tests.test_executor_launch_profile import profile as launch_profile, policy
    raw_profile = profile_canonical(launch_profile(daemon_policy=policy(engine_id=launch.engine_id)))
    # The synthetic supervisor's release is pinned to exactly this synthetic profile.
    profile_digest = 'sha256:' + hashlib.sha256(raw_profile).hexdigest()
    authority.release = replace(authority.release, launch_profile_digest=profile_digest)
    release, launch = current_pins(authority)
    evidence['profile_json'] = raw_profile.decode()
    evidence['release'] = {name: getattr(release, name) for name in release.__dataclass_fields__
                           if name not in {'public_key', 'platform'}}
    evidence['release'].update(public_key_hex=release.public_key.hex(), platform=release.platform.model_dump(mode='json'))
    evidence['launch'] = {name: getattr(launch, name) for name in launch.__dataclass_fields__}
    evidence['report']['executor'] = dict(evidence['launch'], image_config_id=release.image_config_id)
    evidence['report']['expires_at_ms'] = launch.approval_expires_at_ms
    issuer, audience = 'https://fixture.cloudflareaccess.com', 'fixture-audience'
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid='synthetic', alg='RS256', use='sig')
    evidence['operator_actor'] = 'human:cf:' + hashlib.sha256(
        '\0'.join((issuer, audience, 'operator-test')).encode()).hexdigest()
    token = 'synthetic-local-executor-token'
    data['secret_bindings']['executor']['digest'] = 'sha256:' + hashlib.sha256(token.encode()).hexdigest()
    executor_dir = issuer_path.parent / 'executor'
    executor_dir.mkdir(mode=0o700)
    socket_path = executor_dir / 'executor-http.sock'
    data['executor_socket_path'] = str(socket_path)
    data['database_path'] = str(issuer_path.parent / 'controller' / 'hub.sqlite3')
    data['coordinator_socket_path'] = str(issuer_path.parent / 'browser' / 'hub.sock')
    data['secret_root'] = str(issuer_path.parent / 'secrets')
    config = parse_configuration(canonical(data))
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path)); os.chmod(socket_path, 0o600); listener.listen(4); listener.settimeout(.2)
    server_stop = threading.Event()
    relay = UnixCurrentObservationTransport(socket_path=str(issuer_path),
        expected_issuer_uid=os.geteuid(), expected_issuer_gid=os.getegid())
    adapter = NoCommands()
    executor = executor_app(adapter, bearer_token=token, current_observation_transport=relay)
    failures, observations = [], []
    def serve_once():
        try:
            while not server_stop.is_set():
                try: peer, _ = listener.accept()
                except socket.timeout: continue
                with peer:
                    peer.settimeout(3)
                    data = b''
                    while b'\r\n\r\n' not in data:
                        chunk = peer.recv(4096)
                        if not chunk: raise ValueError('short request')
                        data += chunk
                    header, body = data.split(b'\r\n\r\n', 1)
                    length = int(next(line.split(b':', 1)[1] for line in header.split(b'\r\n')
                                      if line.lower().startswith(b'content-length:')))
                    while len(body) < length:
                        chunk = peer.recv(length - len(body))
                        if not chunk: raise ValueError('short body')
                        body += chunk
                    with asyncio.Runner() as runner: events = runner.run(request(executor, header, body))
                    start, end = events
                    assert start['status'] == 200
                    observations.append(True)
                    peer.sendall(b'HTTP/1.1 200 OK\r\n' + b'\r\n'.join(k + b': ' + v for k, v in start['headers'])
                                 + b'\r\n\r\n' + end['body'])
        except BaseException as error: failures.append(error)
    thread = threading.Thread(target=serve_once)
    composition = None
    try:
        with owned_supervisor_wait(authority):
            database = issuer_path.parent / 'hub.sqlite3'
            with PilotOwner(database, allow_ephemeral_test_storage=True) as owner:
                config = replace(config, database=str(database))
                composition = TrustedLocalQueuedSession(config=config, enrollment=parse_enrollment(encode(evidence)),
                                                        credential=token.encode())
                target = ContainerPilotTarget.from_manifest(manifest)
                gate = composition.reconciliation_factory(owner, target)
                repo = HubRepository(database, execution_principal='executor:container-pilot', pilot_owner=owner)
                client = DistinctContainerClient({'smoke_container': 'failed'} if rollback else None)
                workflow = ContainerPilotWorkflow(repo, client, [target], pilot_owner=owner)
                queue = WorkQueue(repo, {ACTION: WorkAction(MACHINE, project='engineering-hub')})
                bridge = QueuedContainerPilot(queue, workflow, action_key=ACTION, machine_id=MACHINE,
                                              reconciliation=gate)
                feed = NativeHubEventFeed(database)
                runtime = QueuedPilotRuntime(bridge, feed)
                auth = AuthConfig('https://hub.example', human_auth_mode='cloudflare_access',
                    cloudflare_issuer=issuer, cloudflare_audience=audience,
                    cloudflare_allowed_emails=('operator@example.test', 'other@example.test'))
                verifier = access.CloudflareAccessVerifier(auth.cloudflare_config,
                    transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'keys': [jwk]})))
                app = create_app(Settings(database, AuthSettings('h', 'c', claude_token='cl'), auth,
                    staging_machine_id='staging-pc'), repository=repo, event_feed=feed, work_queue=queue,
                    queued_pilot_runtime=runtime, access_verifier=verifier)
                composition.session_acceptance_factory(app, owner, repo, target, gate)
                now = int(time.time())
                assertion = jwt.encode({'iss': issuer, 'aud': [audience], 'sub': 'operator-test',
                    'email': 'operator@example.test', 'type': 'app', 'iat': now - 1, 'exp': now + 300},
                    private, algorithm='RS256', headers={'kid': 'synthetic'})
                other_assertion = jwt.encode({'iss': issuer, 'aud': [audience], 'sub': 'different-operator',
                    'email': 'other@example.test', 'type': 'app', 'iat': now - 1, 'exp': now + 300},
                    private, algorithm='RS256', headers={'kid': 'synthetic'})
                route = '/api/hub/v1/staging/session-reconciliation'
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=auth.origin) as other:
                    other_headers = {'Cf-Access-Jwt-Assertion': other_assertion}
                    assert (await other.get('/api/hub/v1/auth/login', headers=other_headers,
                                            follow_redirects=False)).status_code == 303
                    assert (await other.get(route, headers=other_headers)).status_code == 403
                    assert gate.admission_state == 'pending'
                headers = {'Cf-Access-Jwt-Assertion': assertion}
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=auth.origin) as browser:
                    assert (await browser.get(route)).status_code == 401
                    assert (await browser.get('/api/hub/v1/auth/login', headers=headers, follow_redirects=False)).status_code == 303
                    me = (await browser.get('/api/hub/v1/auth/me', headers=headers)).json()
                    headers.update({'Origin': auth.origin, 'X-Hub-CSRF': me['csrf_token']})
                    snapshot = await browser.get(route, headers=headers)
                    assert snapshot.status_code == 200 and gate.admission_state == 'pending'
                    decision = {'request_id': snapshot.json()['request_id'],
                                'expected_revision': snapshot.json()['revision'], 'action': 'accept'}
                    assert (await browser.post(route, headers={**headers, 'X-Hub-CSRF': 'wrong'},
                                               json=decision)).status_code == 403
                    assert gate.admission_state == 'pending'
                    thread.start()
                    response = await browser.post(route, headers=headers, json=decision)
                    assert response.status_code == 200 and gate.admission_state == 'accepted', failures
                try:
                    await feed.start()
                    assert feed.available
                    await runtime.start()
                    from engineering_hub.work_queue import WorkTarget
                    task = repo.create_task('human:1234', 'engineering-hub', 'Synthetic pilot', 'signed-dispatch')
                    task = repo.claim_task('codex', task['id'], task['revision'])
                    task = repo.set_candidate('codex', task['id'], task['revision'], target.candidate_commit)
                    task = repo.review_task('claude', task['id'], task['revision'], target.candidate_commit, 'approve')
                    job = queue.submit(actor='codex', request_id='signed-dispatch', action_key=ACTION,
                        environment='staging', target=WorkTarget(task['id'], target.candidate_commit, target.artifact_digest))
                    async def wait_done():
                        while True:
                            result = queue.get_job(job['id'])
                            if result['state'] in {'succeeded', 'failed', 'held'}: return result
                            await asyncio.sleep(.01)
                    result = await asyncio.wait_for(wait_done(), timeout=10)
                    assert result['id'] == job['id'] and result['state'] == ('failed' if rollback else 'succeeded')
                finally:
                    try:
                        await runtime.stop()
                    finally:
                        await feed.stop()
                expected = ['deploy_container', 'smoke_container']
                if rollback: expected += ['rollback_container', 'smoke_container_rollback']
                assert [command.action for command in client.submissions] == expected
                run = repo.list_runs()[0]
                assert run['status'] == ('rolled_back' if rollback else 'succeeded')
                if rollback:
                    with repo._connection() as db:
                        rows = db.execute('SELECT action,response_json FROM hub_container_pilot_commands WHERE run_id=?',
                                          (run['id'],)).fetchall()
                    receipts = {row['action']: json.loads(row['response_json']) for row in rows}
                    assert receipts['deploy_container']['evidence']['container_id'] == 'e' * 64
                    assert receipts['smoke_container']['evidence']['container_id'] == 'e' * 64
                    assert receipts['rollback_container']['evidence']['container_id'] == 'f' * 64
                    assert receipts['smoke_container_rollback']['evidence']['container_id'] == 'f' * 64
                assert len(observations) >= (5 if rollback else 3) and checks and not failures and not adapter.touched
                server_stop.set(); thread.join(3)
                assert not thread.is_alive()
    finally:
        try:
            if composition is not None: composition.close()
        finally:
            try:
                relay.close()
            finally:
                server_stop.set()
                try:
                    if thread.ident is not None:
                        thread.join(4)
                        assert not thread.is_alive()
                finally:
                    listener.close()
