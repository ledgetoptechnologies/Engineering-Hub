"""Owned GitHub split service over real UDS with synthetic signed authority."""
import asyncio
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import os
import socket
import threading
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

pytest.importorskip('fcntl')

from engineering_hub.auth import GitHubIdentityProvider
from engineering_hub.container_pilot_manifest import PilotManifest
from engineering_hub.trusted_queued_session import SCHEMA_V3, parse_enrollment
from hub_container_executor import installed_coordinator as installed
from hub_container_executor import trusted_local_session as local
from hub_container_executor.asgi import create_app as executor_app
from hub_container_executor.launch_transport import UnixCurrentObservationTransport
from tests.test_hub_container_executor_installed_coordinator import github_configuration, canonical
from tests.test_hub_container_executor_launch_supervisor import synthetic_issuer, short_root, root_journal, current_pins, current_ready
from tests.test_hub_current_observation_end_to_end import owned_supervisor_wait, request
from tests.test_hub_trusted_queued_session import v2_document, encode
from tests.test_hub_unified_pilot_startup import profile_document


@pytest.fixture
def owned_resources():
    with ExitStack() as stack:
        yield stack


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper_observation', [False, True])
@pytest.mark.parametrize(('supervised', 'cleanup_failure','with_broker'), [(False, False,False), (True, False,False), (True, True,False), (True,False,True)])
async def test_github_owned_split_coordinator_accepts_signed_session_over_uds(synthetic_issuer, monkeypatch, owned_resources, tamper_observation, supervised, cleanup_failure, with_broker):
    import engineering_hub.pilot_ownership as ownership
    synthetic_filesystem=ownership._local_filesystem
    monkeypatch.setattr(ownership,'_local_filesystem',lambda fd, **_:synthetic_filesystem(fd))
    authority, issuer_path, checks = synthetic_issuer
    data=github_configuration(); data['manifest']=profile_document()['manifest']
    manifest=PilotManifest.from_bytes(canonical(data['manifest']))
    authority.release=replace(authority.release,implementation_commit=manifest.driver.implementation_commit,
                              platform=manifest.runner_binding.platform)
    authority._daemon_pin=replace(authority._daemon_pin,daemon_id=manifest.runner_binding.daemon_identity)
    current_ready(authority)
    _, launch=current_pins(authority)
    from engineering_hub.executor_launch_profile import canonical as profile_canonical
    from tests.test_executor_launch_profile import profile as launch_profile, policy
    raw_profile=profile_canonical(launch_profile(daemon_policy=policy(engine_id=launch.engine_id)))
    authority.release=replace(authority.release,launch_profile_digest='sha256:'+hashlib.sha256(raw_profile).hexdigest())
    release, launch=current_pins(authority)
    evidence=v2_document(); evidence.update(schema=SCHEMA_V3,operator_actor='human:123',profile_json=raw_profile.decode())
    evidence['release']={name:getattr(release,name) for name in release.__dataclass_fields__ if name not in {'public_key','platform'}}
    evidence['release'].update(public_key_hex=release.public_key.hex(),platform=release.platform.model_dump(mode='json'))
    evidence['launch']={name:getattr(launch,name) for name in launch.__dataclass_fields__}
    evidence['report']['executor']=dict(evidence['launch'],image_config_id=release.image_config_id)
    evidence['report']['expires_at_ms']=launch.approval_expires_at_ms
    token='synthetic-local-executor-token'
    tokens={'codex':'synthetic-codex','hermes':'synthetic-hermes','claude':'synthetic-claude','executor':token,'github':'synthetic-github'}
    for role,value in tokens.items(): data['secret_bindings'][role]['digest']='sha256:'+hashlib.sha256(value.encode()).hexdigest()
    root=issuer_path.parent
    executor_dir=root/'executor'; executor_dir.mkdir(mode=0o700)
    (root/'browser').mkdir(mode=0o700)
    (root/'controller').mkdir(mode=0o700)
    executor_path=executor_dir/'executor.sock'
    data.update(executor_socket_path=str(executor_path),database_path=str(root/'controller'/'hub.sqlite3'),
                coordinator_socket_path=str(root/'browser'/'hub.sock'),secret_root=str(root/'secrets'))
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0)); data['agent_port']=probe.getsockname()[1]
    config=installed.parse_configuration(canonical(data))
    enrollment=parse_enrollment(encode(evidence))
    class Secrets:
        def __init__(self, root, *, bindings): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def resolve_test_secret(self, slot, version): return tokens[slot.removeprefix('test-')]
    monkeypatch.setattr(installed,'ImmutableTestSecretResolver',Secrets)
    # The test owns this process; it is not an installed UID10001/PID1 service.
    monkeypatch.setattr(installed,'_process',lambda:None)
    admission_verifier = None
    if supervised:
        from hub_container_executor import runtime_identity
        class TestAdmission:
            def implementation_commit(self): return 'a'*40
        monkeypatch.setattr(runtime_identity, 'CoordinatorLaunchReceiptIdentityVerifier', TestAdmission)
        admission_verifier = TestAdmission()
    async def exchange(self, code, verifier):
        assert code=='synthetic-code' and verifier
        return 123
    if not with_broker: monkeypatch.setattr(GitHubIdentityProvider,'exchange',exchange)
    listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    owned_resources.callback(listener.close)
    listener.bind(str(executor_path));os.chmod(executor_path,0o600);listener.listen(4);listener.settimeout(.2)
    relay=UnixCurrentObservationTransport(socket_path=str(issuer_path),expected_issuer_uid=os.geteuid(),expected_issuer_gid=os.getegid())
    from engineering_hub.container_pilot_client import ContainerPilotResponse, ContainerPilotEvidence
    class SyntheticAdapter:
        def __init__(self): self.commands = []; self.fence = None
        def submit(self, command):
            if command.manifest_digest != manifest.digest() or (self.fence is not None and command.fencing_generation != self.fence):
                return ContainerPilotResponse(**command.model_dump(), state='held', reason='identity_conflict', replayed=False,
                    evidence=ContainerPilotEvidence(container_id=None, image_config_id=None, health='unknown', evidence_verified=False))
            self.fence = command.fencing_generation
            self.commands.append(command)
            return ContainerPilotResponse(**command.model_dump(), state='succeeded', reason=None, replayed=False,
                evidence=ContainerPilotEvidence(container_id='e'*64, image_config_id=manifest.candidate.image_config_id,
                                                health='healthy', evidence_verified=True))
    class Observation:
        def observe_current_launch(self, **kwargs):
            raw = relay.observe_current_launch(**kwargs)
            if not tamper_observation: return raw
            import json
            envelope = json.loads(raw)
            envelope['payload']['nonce'] = '0' * 64
            return json.dumps(envelope, sort_keys=True, separators=(',', ':')).encode()
    adapter=SyntheticAdapter(); executor=executor_app(adapter,bearer_token=token,current_observation_transport=Observation())
    stop_thread=threading.Event(); errors=[]; observations=[]
    def worker():
        try:
            while not stop_thread.is_set():
                try: peer,_=listener.accept()
                except socket.timeout: continue
                with peer:
                    peer.settimeout(3); raw=b''
                    while b'\r\n\r\n' not in raw:
                        chunk=peer.recv(4096)
                        if not chunk: raise ValueError('short request')
                        raw+=chunk
                    header,body=raw.split(b'\r\n\r\n',1)
                    length=int(next(line.split(b':',1)[1] for line in header.split(b'\r\n') if line.lower().startswith(b'content-length:')))
                    while len(body)<length:
                        chunk=peer.recv(length-len(body))
                        if not chunk: raise ValueError('short body')
                        body+=chunk
                    with asyncio.Runner() as runner: start,end=runner.run(request(executor,header,body))
                    observations.append(start['status'])
                    peer.sendall(b'HTTP/1.1 '+str(start['status']).encode()+b' OK\r\n'+b'\r\n'.join(k+b': '+v for k,v in start['headers'])+b'\r\n\r\n'+end['body'])
        except BaseException as error: errors.append(error)
    thread=threading.Thread(target=worker); stop=asyncio.Event(); ready=asyncio.get_running_loop().create_future()
    original=installed.serve_owned_queued_pilot
    async def serve(*args,**kwargs):
        return await original(*args,**kwargs,stop_event=stop,on_ready=lambda *a:ready.set_result(a),install_signals=False)
    monkeypatch.setattr(installed,'serve_owned_queued_pilot',serve)
    session=local.TrustedLocalQueuedSession(config=config,enrollment=enrollment,credential=token.encode())
    owned_resources.callback(session.close)
    owned_resources.enter_context(owned_supervisor_wait(authority))
    serving=None; broker_task=None; broker_stop=None; broker=None; oauth_client=None
    try:
        oauth_options={}
        if with_broker:
            from engineering_hub.github_oauth_broker import GitHubOAuthBroker,GitHubOAuthUnixClient,serve_github_oauth_broker
            from hub_container_executor import coordinator_admission
            from hub_container_executor.coordinator_admission import GitHubBrokerEnrollment
            oauth_path=str(root/'oauth.sock'); provider_calls=[]
            # Test-only endpoint is task-private; production path remains fixed.
            monkeypatch.setattr(coordinator_admission,'OAUTH_SOCKET',oauth_path)
            async def github(request):
                provider_calls.append((request.method,str(request.url)))
                return httpx.Response(200,json={'access_token':'test-only-token'} if request.method=='POST' else {'id':123})
            broker=GitHubOAuthBroker(client_id='fixture-client',client_secret=tokens['github'],
                secret_sha256='sha256:'+hashlib.sha256(tokens['github'].encode()).hexdigest(),origin='https://pilot.example.test',
                peer_uid=os.geteuid(),peer_gid=os.getegid(),transport=httpx.MockTransport(github))
            broker_listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            owned_resources.callback(broker_listener.close)
            broker_listener.bind(oauth_path);broker_listener.listen();broker_listener.setblocking(False)
            broker_stop=asyncio.Event()
            broker_task=asyncio.create_task(serve_github_oauth_broker(broker_listener,broker,broker_stop))
            oauth_client=GitHubOAuthUnixClient(socket_path=oauth_path,issuer_uid=os.geteuid(),issuer_gid=os.getegid())
            binding=GitHubBrokerEnrollment(oauth_path,os.geteuid(),os.getegid(),'fixture-client','https://pilot.example.test',
                                           'sha256:'+hashlib.sha256(tokens['github'].encode()).hexdigest())
            oauth_options={'github_client':oauth_client,'oauth_binding':binding}
        thread.start()
        serving=asyncio.create_task(installed.run_installed_coordinator(config,reconciliation_factory=session.reconciliation_factory,
                                               session_acceptance_factory=session.session_acceptance_factory,
                                               admission_verifier=admission_verifier,**oauth_options))
        done,_=await asyncio.wait({ready,serving},timeout=10,return_when=asyncio.FIRST_COMPLETED)
        if serving in done: await serving
        assert ready in done
        transport=httpx.AsyncHTTPTransport(uds=str(config.coordinator_socket))
        async with httpx.AsyncClient(transport=transport,base_url='http://pilot.example.test',follow_redirects=False,timeout=8) as browser:
            route='/api/hub/v1/staging/session-reconciliation'
            assert (await browser.get(route)).status_code==401
            login=await browser.get('/api/hub/v1/auth/login');assert login.status_code==302
            state=parse_qs(urlsplit(login.headers['location']).query)['state'][0]
            oauth_cookie=browser.cookies.get('hub_session_oauth'); assert oauth_cookie
            callback=await browser.get('/api/hub/v1/auth/callback',params={'state':state,'code':'synthetic-code'},
                                       headers={'Cookie':'hub_session_oauth='+oauth_cookie})
            assert callback.status_code==303
            if with_broker: assert provider_calls==[('POST','https://github.com/login/oauth/access_token'),('GET','https://api.github.com/user')]
            cookie=browser.cookies.get('hub_session');assert cookie
            browser.headers['Cookie']='hub_session='+cookie
            me=(await browser.get('/api/hub/v1/auth/me')).json();assert me['authenticated'] and me['actor']=='human:123'
            snapshot=await browser.get(route);assert snapshot.status_code==200
            decision={'request_id':snapshot.json()['request_id'],'expected_revision':snapshot.json()['revision'],'action':'accept'}
            assert (await browser.post(route,json=decision)).status_code==403
            accepted=await browser.post(route,json=decision,headers={'Origin':'https://pilot.example.test','X-Hub-CSRF':me['csrf_token']})
            if tamper_observation:
                assert accepted.status_code == 503
                assert accepted.json()['detail']['code'] == 'session_acceptance_unavailable'
                assert not adapter.commands and observations and all(s == 200 for s in observations) and checks and not errors
                return
            assert accepted.status_code==200
            headers={'Origin':'https://pilot.example.test','X-Hub-CSRF':me['csrf_token']}
            task_response=await browser.post('/api/hub/v1/tasks',headers=headers,json={'project':'engineering-hub',
                'title':'Signed local UDS pilot','client_request_id':'signed-local-uds'})
            assert task_response.status_code==201
            task=task_response.json(); task_path='/api/hub/v1/tasks/'+task['id']
            agent_address=ready.result()[0]
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{agent_address[1]}',trust_env=False,
                    headers={'Host':f'127.0.0.1:{config.port}','Authorization':'Bearer '+tokens['codex']}) as agent:
                claim=await agent.post(task_path+'/claim',json={'expected_revision':task['revision']}); assert claim.status_code==200
                candidate=await agent.post(task_path+'/candidate',json={'expected_revision':claim.json()['revision'],
                    'commit':manifest.candidate.source_commit}); assert candidate.status_code==200
                reviewed=await agent.post(task_path+'/review',headers={'Authorization':'Bearer '+tokens['claude']},
                    json={'expected_revision':candidate.json()['revision'],'commit':manifest.candidate.source_commit,'verdict':'approve'})
                assert reviewed.status_code==200
                job=await agent.post('/api/hub/v1/work/jobs',json={'request_id':'signed-local-uds',
                    'action_key':installed.ACTION,'environment':'staging','target':{'task_id':task['id'],
                    'source_commit':manifest.candidate.source_commit,'artifact_digest':manifest.candidate.image_config_id}})
                assert job.status_code==201
                async with asyncio.timeout(10):
                    while True:
                        state=(await browser.get('/api/hub/v1/work/jobs/'+job.json()['id'])).json()['state']
                        if state in {'succeeded','failed','held'}: break
                        await asyncio.sleep(.05)
                assert state=='succeeded'
        assert [command.action for command in adapter.commands]==['deploy_container','smoke_container']
        assert all(command.manifest_digest==manifest.digest() and command.fencing_generation>0 for command in adapter.commands)
        deploy, smoke = adapter.commands
        for field in ('run_id', 'target_id', 'policy_revision', 'candidate_commit', 'fencing_generation'):
            assert getattr(deploy, field) == getattr(smoke, field)
        from uuid import UUID
        assert deploy.command_id != smoke.command_id
        assert all(str(UUID(value)) == value for value in (deploy.run_id, deploy.command_id, smoke.command_id))
        assert deploy.target_id == 'engineering-hub:staging'
        assert deploy.candidate_commit == manifest.candidate.source_commit
        # Exercise the same private HTTP endpoint with altered authority; the
        # synthetic adapter models denial and must record no extra effect.
        from engineering_hub.container_pilot_client import ContainerPilotClient
        direct=ContainerPilotClient.for_private_unix(config.executor_socket,token)
        try:
            stale=adapter.commands[0].model_copy(update={'fencing_generation':adapter.commands[0].fencing_generation+1})
            assert (await direct.submit_command(stale)).state=='held'
            tampered=adapter.commands[0].model_copy(update={'manifest_digest':'sha256:'+'0'*64})
            assert (await direct.submit_command(tampered)).state=='held'
        finally:
            await direct.aclose()
        assert len(adapter.commands)==2
        assert observations and all(status==200 for status in observations) and checks and not errors
    finally:
        if cleanup_failure:
            from engineering_hub.queued_pilot_runtime import QueuedPilotRuntime
            original_stop = QueuedPilotRuntime.stop
            async def failed_stop(runtime):
                await original_stop(runtime)
                raise RuntimeError('synthetic cleanup failure')
            monkeypatch.setattr(QueuedPilotRuntime, 'stop', failed_stop)
        stop.set()
        try:
            if serving is not None:
                import sys
                active_error=sys.exc_info()[1]
                outcome=await asyncio.wait_for(asyncio.gather(serving,return_exceptions=True),10)
                if active_error is None and cleanup_failure:
                    assert isinstance(outcome[0], BaseException)
                elif active_error is None and isinstance(outcome[0],BaseException): raise outcome[0]
                if active_error is None and supervised and not cleanup_failure:
                    from hub_container_executor.coordinator_drain import LocalCoordinatorDrain
                    assert type(outcome[0]) is LocalCoordinatorDrain and outcome[0].manifest_digest == manifest.digest()
        finally:
            stop_thread.set()
            try:
                if thread.ident is not None: thread.join(4); assert not thread.is_alive()
            finally:
                if broker_task is not None:
                    broker_stop.set();await broker_task
                if oauth_client is not None:oauth_client.close()
                if broker is not None:broker.close()
