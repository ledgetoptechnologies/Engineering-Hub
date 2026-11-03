"""Installed selector through real manual acceptance; synthetic local host facts.

The listener bind, PID/UID guard and Docker command endpoint are test boundaries.
This is not proof of executor socket identity, Docker deployment or NAS state.
"""
import asyncio
from dataclasses import replace
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

pytest.importorskip('fcntl', reason='Linux owner and socket fixtures')

from engineering_hub import cloudflare_access as access
from engineering_hub import pilot_ownership as ownership
from engineering_hub import queued_pilot_service as serving
from engineering_hub import trusted_queued_session as session
from engineering_hub.container_pilot_client import ContainerPilotClient
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.unified_pilot_startup import parse_profile
from hub_container_executor import installed_coordinator as installed
from hub_container_executor.asgi import create_app as executor_app
from hub_container_executor.launch_transport import UnixCurrentObservationTransport
from hub_container_executor.local_providers import ImmutableTestSecretResolver
from tests.test_hub_container_executor_installed_coordinator import configuration, canonical
from tests.test_hub_container_executor_launch_supervisor import (
    short_root, root_journal, synthetic_issuer, current_ready, current_pins,
)
from tests.test_hub_current_executor_transport import material, server, TOKEN
from tests.test_hub_current_observation_end_to_end import owned_supervisor_wait, request, NoCommands
from tests.test_hub_trusted_queued_session import document
from tests.test_hub_unified_pilot_startup import profile_document, encode


@pytest.mark.asyncio
@pytest.mark.parametrize('drain_case', ['verified', 'unproved', 'pilot-success', 'pilot-held', 'pilot-rollback'])
async def test_installed_selector_real_owner_acceptance_with_signed_synthetic_observer(
        synthetic_issuer, material, tmp_path, monkeypatch, drain_case):
    if drain_case == 'unproved' and os.environ.get('EH_HOLD_CHILD') != '1':
        env = dict(os.environ, EH_HOLD_CHILD='1')
        with tempfile.TemporaryDirectory(prefix='s-', dir='/tmp') as child_dir:
            result = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-s',
                str(Path(__file__).resolve()) + '::test_installed_selector_real_owner_acceptance_with_signed_synthetic_observer[unproved]',
                '--basetemp', child_dir, '-p', 'no:cacheprovider'],
                env=env, capture_output=True, text=True, timeout=35)
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'UNPROVED_HOLD_ASSERTIONS_PASSED' in result.stdout
        return
    authority, issuer_path, checks = synthetic_issuer
    profile_data = profile_document()
    manifest = parse_profile(encode(profile_data)).manifest
    authority.release = replace(authority.release,
        implementation_commit=manifest.driver.implementation_commit,
        platform=manifest.runner_binding.platform)
    authority._daemon_pin = replace(authority._daemon_pin,
        daemon_id=manifest.runner_binding.daemon_identity)
    current_ready(authority)
    release, launch = current_pins(authority)
    relay = UnixCurrentObservationTransport(socket_path=str(issuer_path),
        expected_issuer_uid=os.geteuid(), expected_issuer_gid=os.getegid())
    from tests.test_hub_queued_pilot import DistinctContainerClient
    from tests.test_hub_container_pilot import reviewed_task, ACTIONS
    from engineering_hub.container_pilot_client import ContainerPilotRequest
    from engineering_hub.work_queue import WorkTarget
    from engineering_hub.repository import HubRepository
    fake = DistinctContainerClient()
    if drain_case == 'pilot-held':
        fake.outcomes['deploy_container'] = 'held'
    if drain_case == 'pilot-rollback':
        fake.outcomes['smoke_container'] = 'failed'
    commands, inspections = [], []
    async def command_transport(request):
        if not drain_case.startswith('pilot-'):
            pytest.fail('No executor commands permitted')
        if request.method == 'POST':
            command = ContainerPilotRequest.model_validate(json.loads(await request.aread()))
            commands.append(command)
        else:
            identity = request.url.path.rsplit('/', 1)[-1]
            command = next(item for item in commands if item.command_id == identity)
            inspections.append(identity)
        return httpx.Response(200, json=fake.response(command).model_dump(mode='json'))
    no_commands = NoCommands()
    executor = executor_app(no_commands, bearer_token=TOKEN.decode(), current_observation_transport=relay)

    def respond(conn, _):
        header, body = seen[-1]
        with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
            start, end = runner.run(request(executor, header, body))
        assert start['status'] == 200
        conn.sendall(b'HTTP/1.1 200 OK\r\n' + b'\r\n'.join(
            key + b': ' + value for key, value in start['headers']) + b'\r\n\r\n' + end['body'])

    # The real secret resolver is used with test-only ephemeral storage.
    resolver = ImmutableTestSecretResolver
    monkeypatch.setattr(installed, 'ImmutableTestSecretResolver',
        lambda root, *, bindings: resolver(root, bindings=bindings, allow_ephemeral_test_storage=True))
    monkeypatch.setattr(installed, '_process', lambda: None)  # Test process is not production PID 1/UID 10001.
    owner_class = PilotOwner
    monkeypatch.setattr(serving, 'PilotOwner',
        lambda path: owner_class(path, allow_ephemeral_test_storage=True))
    observed_filesystem = ownership._local_filesystem
    monkeypatch.setattr(ownership, '_local_filesystem', lambda fd, **_: observed_filesystem(fd))
    monkeypatch.setattr(ContainerPilotClient, 'for_private_unix', classmethod(
        lambda cls, path, token: cls('http://127.0.0.1', token,
            transport=httpx.MockTransport(command_transport))))

    issuer, audience, owner_sub = 'https://fixture.cloudflareaccess.com', 'fixture-pilot', 'owner-fixture'
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    key.update(kid='fixture-key', alg='RS256', use='sig')
    verifier_type = access.CloudflareAccessVerifier
    monkeypatch.setattr(access, 'CloudflareAccessVerifier', lambda config:
        verifier_type(config, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'keys': [key]}))))
    def assertion(sub, email):
        now = int(time.time())
        return jwt.encode(dict(iss=issuer, aud=[audience], sub=sub, email=email,
            type='app', iat=now-1, exp=now+300), private, algorithm='RS256', headers={'kid': 'fixture-key'})

    config_data = configuration()
    config_data.update(manifest=profile_data['manifest'], supervisor_stop_grace_seconds=2000,
        database_path=str(tmp_path/'database'/'hub.sqlite3'),
        coordinator_socket_path=str(tmp_path/'browser'/'hub.sock'),
        executor_socket_path=str(tmp_path/'executor'/'host.sock'), secret_root=str(tmp_path/'secrets'))
    config_data['public_auth'] = dict(base_url='https://pilot.example.test', cloudflare_issuer=issuer,
        cloudflare_audience=audience, cloudflare_allowed_emails=['owner@example.test', 'other@example.test'])
    for dirname in ('database', 'browser', 'executor', 'secrets'):
        (tmp_path/dirname).mkdir(mode=0o700)
    for role, binding in config_data['secret_bindings'].items():
        raw = ('synthetic-' + role + '-only').encode()
        digest = hashlib.sha256(raw).hexdigest()
        binding['digest'] = 'sha256:' + digest
        file = tmp_path/'secrets'/digest
        file.write_bytes(raw); file.chmod(0o600)
    config = installed.parse_configuration(canonical(config_data))
    if drain_case.startswith('pilot-'):
        with owner_class(config.database, allow_ephemeral_test_storage=True) as seed_owner:
            seed_repo = HubRepository(config.database, execution_principal='executor:container-pilot', pilot_owner=seed_owner)
            seed_task = reviewed_task(seed_repo, 'synthetic-selector-pilot')
        profile_data['allowed_task_ids'] = [seed_task['id']]
    seen_acceptance = []
    retained = {'cleanup_seen': asyncio.Event()}
    release_operation = asyncio.Event()

    async def serve_private(pair_factory, **_kwargs):
        # Real service pair factory, repository, gate, broker, audit and ASGI.
        # Only the socket listener is replaced; HTTP is in-process.
        pair = pair_factory()
        core = pair.core
        retained['owner'] = core.state.repository.pilot_owner
        gate = core.state.queued_pilot_runtime.bridge.reconciliation
        assert gate.admission_state == 'pending'
        async with pair.lifespan():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=core),
                    base_url='https://pilot.example.test', follow_redirects=False) as browser:
                async def login(sub, email):
                    browser.cookies.clear(); browser.headers.clear()
                    browser.headers['Cf-Access-Jwt-Assertion'] = assertion(sub, email)
                    assert (await browser.get('/api/hub/v1/auth/login')).status_code == 303
                    me = (await browser.get('/api/hub/v1/auth/me')).json()
                    browser.headers.update({'Origin': 'https://pilot.example.test', 'X-Hub-CSRF': me['csrf_token']})
                path = '/api/hub/v1/staging/session-reconciliation'
                await login('other-fixture', 'other@example.test')
                assert (await browser.get(path)).status_code == 403
                assert gate.admission_state == 'pending'
                await login(owner_sub, 'owner@example.test')
                snap = (await browser.get(path)).json()
                response = await browser.post(path, json={'request_id': snap['request_id'],
                    'expected_revision': snap['revision'], 'action': 'accept'})
                assert response.status_code == 200 and response.json()['execution_authorization'] is False
                assert gate.admission_state == 'accepted'
                with core.state.repository._connection() as db:
                    assert db.execute('SELECT count(*) FROM hub_session_acceptance_audit').fetchone()[0] == 2
                assert checks and seen and not no_commands.touched
                seen_acceptance.append(True)
                if drain_case.startswith('pilot-'):
                    queue = core.state.work_queue
                    runtime = core.state.queued_pilot_runtime
                    job = queue.submit(actor='codex', request_id='synthetic-selector-job',
                        action_key=installed.ACTION, environment='staging',
                        target=WorkTarget(seed_task['id'], manifest.candidate.source_commit,
                                          manifest.candidate.image_config_id))
                    async with asyncio.timeout(12):
                        while queue.get_job(job['id'])['state'] not in {'succeeded', 'held', 'failed'}:
                            await asyncio.sleep(.01)
                    outcome = queue.get_job(job['id'])
                    if drain_case == 'pilot-success':
                        assert outcome['state'] == 'succeeded'
                        assert [item.action for item in commands] == ACTIONS[:2]
                    elif drain_case == 'pilot-rollback':
                        assert outcome['state'] == 'failed'
                        assert [item.action for item in commands] == ACTIONS
                        assert core.state.repository.list_runs()[0]['status'] == 'rolled_back'
                    else:
                        assert outcome['state'] == 'held'
                        assert [item.action for item in commands] == ACTIONS[:1]
                        before = len(commands)
                        recovered = await runtime.bridge.recover(job['id'])
                        assert recovered['state'] == 'held'
                        assert inspections == [commands[0].command_id]
                        assert len(commands) == before
                        assert queue.get_job(job['id'])['state'] == 'held'
                        # No replacement POST even if recovery cannot get a
                        # trustworthy terminal response from the synthetic host.
                        await asyncio.sleep(.2)
                        assert len(commands) == before
                    assert len(core.state.repository.list_runs()) == 1
                    run = core.state.repository.list_runs()[0]
                    with core.state.repository._connection() as db:
                        reservation = db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=?',
                                                 ('staging-pc',)).fetchone()
                    assert reservation is not None
                    assert reservation['job_id'] == (job['id'] if drain_case == 'pilot-held' else None)
                    assert all(command.run_id == run['id']
                               and command.target_id == run['target']
                               and command.candidate_commit == run['commit']
                               and command.manifest_digest == run['execution_manifest_digest']
                               and command.fencing_generation == run['fencing_generation']
                               for command in commands)
                    assert runtime.status['running']
                if drain_case == 'unproved':
                    async def accepted_operation():
                        await release_operation.wait()
                        retained['owner'].assert_active(core.state.repository.path)
                    retained['operation'] = asyncio.create_task(accepted_operation())
                    await asyncio.sleep(0)
                    async def unproved_shared_drain(_shared, _draft):
                        # Exercise the real service's owner-retention branch.
                        # This fixture simulates its inability to prove drain;
                        # it does not claim a real staging operation ran.
                        assert not retained['operation'].done()
                        retained['owner'].assert_active(core.state.repository.path)
                        retained['cleanup_seen'].set()
                        return False, True
                    monkeypatch.setattr(serving, '_cleanup_shared_staging', unproved_shared_drain)
    monkeypatch.setattr(serving, 'serve_private_coordinator', serve_private)

    compositions = []
    original_init, original_close = session.TrustedQueuedSessionComposition.__init__, session.TrustedQueuedSessionComposition.close
    def capture_init(self, **kwargs):
        original_init(self, **kwargs); compositions.append(self)
    def capture_close(self):
        original_close(self)
    monkeypatch.setattr(session.TrustedQueuedSessionComposition, '__init__', capture_init)
    monkeypatch.setattr(session.TrustedQueuedSessionComposition, 'close', capture_close)

    try:
        with owned_supervisor_wait(authority), server(material, respond) as (origin, seen):
            profile_data['executor'] = {'https_origin': origin,
                **{name: str(material[name]) if name.endswith('_file') else material[name]
                   for name in ('ca_file', 'ca_sha256', 'credential_file', 'credential_sha256')}}
            enrollment = document()
            enrollment['operator_actor'] = 'human:cf:' + hashlib.sha256(
                '\0'.join((issuer, audience, owner_sub)).encode()).hexdigest()
            enrollment['release'] = {name: getattr(release, name)
                for name in release.__dataclass_fields__ if name not in {'public_key', 'platform'}}
            enrollment['release'].update(public_key_hex=release.public_key.hex(), platform=release.platform.model_dump(mode='json'))
            enrollment['launch'] = {name: getattr(launch, name) for name in launch.__dataclass_fields__}
            enrollment['report']['executor'] = dict(enrollment['launch'], image_config_id=release.image_config_id)
            enrollment['report']['expires_at_ms'] = launch.approval_expires_at_ms
            profile_path, enrollment_path = tmp_path/'pilot.json', tmp_path/'manual-session.json'
            for path, data in ((profile_path, profile_data), (enrollment_path, enrollment)):
                path.write_bytes(encode(data)); path.chmod(0o600)
            operation = installed.run_installed_coordinator(config, session_profile_path=profile_path,
                                                             session_enrollment_path=enrollment_path)
            if drain_case == 'unproved':
                operation = asyncio.create_task(operation)
                await asyncio.wait_for(retained['cleanup_seen'].wait(), 10)
                await asyncio.sleep(0.05)
                retained['owner'].assert_active(config.database)
                assert not retained['operation'].done()
                assert not operation.done()
                assert compositions[0]._closed is False
                for _ in range(2):
                    operation.cancel()
                    await asyncio.sleep(0.05)
                    assert not operation.done()
                    assert compositions[0]._closed is False
                import gc
                gc.collect()
                retained['owner'].assert_active(config.database)
                from engineering_hub.unproved_drain import UnprovedDrainError
                with pytest.raises(UnprovedDrainError):
                    await installed.run_installed_coordinator(config, session_profile_path=profile_path,
                        session_enrollment_path=enrollment_path)
                print('UNPROVED_HOLD_ASSERTIONS_PASSED', flush=True)
                # No fake drain or manual owner release. The isolated child must
                # terminate to end a deliberately irreversible fail-closed hold.
                os._exit(0)
            else:
                await operation
        assert seen_acceptance == [True]
        assert len(compositions) == 1
        if drain_case == 'verified':
            assert compositions[0]._closed is True
    finally:
        release_operation.set()
        if 'operation' in retained:
            await retained['operation']
            retained['owner'].close()
        for composition in compositions:
            composition.close()
        relay.close()
