"""Private provider contract tests; fake controller, never real Docker."""
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import subprocess
import threading

import pytest

from engineering_hub.development_ci import DevelopmentCIAttempt, DevelopmentCIManifest
from engineering_hub.development_ci_local import LocalDevelopmentCIProvider, _load_controller
from engineering_hub import development_ci_local as local
from tests.test_local_development_ci import hooked_controller, kernel_scope, runner

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux installed controller boundary')


@pytest.fixture
def provider(tmp_path,monkeypatch):
    # These are fake-controller contract tests, not isolated-runtime acceptance.
    monkeypatch.setattr(local,'_isolated_runtime',lambda:True)
    installed = tmp_path/'installed'
    installed.mkdir(mode=0o700)
    source = installed/'controller.py'
    source.write_text('''from pathlib import Path
FILES={'one':'one','two':'two','three':'three','four':'four'}
def private_directory(path): return Path(path)
''')
    checkout = tmp_path/'candidate'
    checkout.mkdir()
    output = tmp_path/'results'
    output.mkdir(mode=0o700)
    manifest = DevelopmentCIManifest('staging-pc','engine-one','sha256:'+'a'*64,
        'sha256:'+hashlib.sha256(source.read_bytes()).hexdigest(),('sha256:'+'b'*64,)*4)
    result = LocalDevelopmentCIProvider(manifest,controller_path=source,checkout=checkout,
        development_ref='refs/heads/codex/candidate',engine_socket='unix:///private/docker.sock',output_root=output)
    result.controller.engine_socket = lambda _v: None
    result.controller.validate_engine = lambda row, expected: None
    result.controller.verify_created = lambda *a: None
    value = dict(source_commit='c'*40,image_id=manifest.image_id,engine_id=manifest.engine_id,
        profile='all',terminal_verified=True,cgroup_terminal={'descendants_empty':True},
        container_id='d'*64,exit_code=0,container_name='owned',input_directory='/private/input',intent='owned',
        timed_out=False,failed=False)
    row = dict(Id='d'*64,State=dict(Status='exited',Pid=0,Running=False,Paused=False,Restarting=False,ExitCode=0))
    def docker(*parts):
        if parts[0]=='info': return {'ID':manifest.engine_id}
        if parts[0]=='image':
            return [dict(Id=manifest.image_id,Config={'Labels':{
                'io.engineering-hub.ci.'+label+'-sha256':'b'*64 for label in result.controller.FILES.values()}})]
        return [row]
    result._docker = docker
    def run(args, *, expected_commit, admission_guard, terminal_observer):
        assert expected_commit=='c'*40
        admission_guard()
        terminal_observer(value)
        return {'advisory':True}
    result.controller.run = run
    attempt = DevelopmentCIAttempt('attempt-one','job-one',1,'epoch-one','task-one',1,'review','codex','c'*40,manifest.digest)
    return result,attempt,value,row,source


@pytest.mark.asyncio
async def test_private_observation_then_separate_retained_container_inspection(provider):
    p,a,v,row,source=provider
    thread=threading.get_ident()
    checks=[]
    def guard():
        assert threading.get_ident()==thread
        checks.append(True)
    receipt=await p.invoke(a,guard)
    proof=await p.verify_terminal(a,receipt)
    assert proof.attempt==a and proof.exit_code==0 and len(checks)==2
    assert proof.container_id=='d'*64 and proof.descendants_empty
    with pytest.raises(ValueError,match='Original'):
        await p.invoke(a,guard)
    with pytest.raises(ValueError,match='Foreign'):
        await p.verify_terminal(replace(a,fence=2),receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['timeout','cleanup_failure'])
async def test_raw_zero_cannot_hide_timeout_or_failed_cleanup(provider,kind):
    p,a,v,row,_=provider
    v['timed_out' if kind=='timeout' else 'failed']=True
    receipt=await p.invoke(a,lambda:None)
    assert (await p.verify_terminal(a,receipt)).exit_code==124


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value',[('Id','e'*64),('Status','running'),('ExitCode',1),('Running',True),('Pid',42)])
async def test_reinspection_mismatch_never_returns_terminal_proof(provider,field,value):
    p,a,v,row,_=provider
    receipt=await p.invoke(a,lambda:None)
    if field=='Id': row[field]=value
    else: row['State'][field]=value
    with pytest.raises(ValueError): await p.verify_terminal(a,receipt)


@pytest.mark.asyncio
async def test_advisory_result_without_private_observer_is_not_proof(provider):
    p,a,*_=provider
    p.controller.run=lambda *a,**kw:dict(terminal_verified=True,exit_code=0)
    with pytest.raises(ValueError,match='observation'):
        await p.invoke(a,lambda:None)
    assert p._receipts=={}


@pytest.mark.asyncio
async def test_guard_denial_runs_on_loop_and_never_creates_receipt(provider):
    p,a,*_=provider
    count=0
    def guard():
        nonlocal count
        count+=1
        if count==2: raise ValueError('lost admission')
    with pytest.raises(ValueError,match='lost admission'): await p.invoke(a,guard)
    assert not p._receipts


@pytest.mark.asyncio
async def test_repeated_cancellation_drains_thread_before_releasing_provider(provider):
    p,a,v,*_=provider
    loop=asyncio.get_running_loop()
    started=asyncio.Event()
    release=threading.Event()
    def run(args, *, expected_commit,admission_guard,terminal_observer):
        admission_guard()
        loop.call_soon_threadsafe(started.set)
        assert release.wait(3)
        terminal_observer(v)
    p.controller.run=run
    task=asyncio.create_task(p.invoke(a,lambda:None))
    await asyncio.wait_for(started.wait(),2)
    try:
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and p._lock.locked()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert not p._lock.locked() and len(p._receipts)==1


def test_installed_code_digest_and_outside_candidate_boundary(provider):
    p,a,v,row,source=provider
    with pytest.raises(ValueError,match='digest'):
        _load_controller(source,'sha256:'+'0'*64)
    with pytest.raises(ValueError,match='independent'):
        LocalDevelopmentCIProvider(p.manifest,controller_path=source,checkout=source.parent,
            development_ref=p.ref,engine_socket=p.engine,output_root=p.output)
    source.chmod(0o666)
    with pytest.raises(ValueError,match='Protected'):
        _load_controller(source,p.manifest.controller_digest)


def test_nonisolated_runtime_cannot_load_host_code(provider,monkeypatch):
    p,a,v,row,source=provider
    monkeypatch.setattr(local,'_isolated_runtime',lambda:False)
    with pytest.raises(ValueError,match='Isolated'):
        _load_controller(source,p.manifest.controller_digest)


def test_candidate_controller_is_rejected_before_loading(provider,monkeypatch):
    p,a,v,row,source=provider
    monkeypatch.setattr(local,'_load_controller',lambda *a:pytest.fail('candidate code executed'))
    with pytest.raises(ValueError,match='independent'):
        LocalDevelopmentCIProvider(p.manifest,controller_path=source,checkout=source.parent,
            development_ref=p.ref,engine_socket=p.engine,output_root=p.output)


def test_candidate_import_path_is_rejected_before_loading(provider,monkeypatch):
    p,a,v,row,source=provider
    monkeypatch.setattr(local,'_load_controller',lambda *a:pytest.fail('candidate import permitted'))
    monkeypatch.syspath_prepend(str(p.checkout))
    with pytest.raises(ValueError,match='import path'):
        LocalDevelopmentCIProvider(p.manifest,controller_path=source,checkout=p.checkout,
            development_ref=p.ref,engine_socket=p.engine,output_root=p.output)


def test_actual_controller_loads_in_isolated_interpreter_without_invocation(tmp_path):
    installed=tmp_path/'enrolled'
    installed.mkdir(mode=0o700)
    root=Path(__file__).resolve().parents[1]
    source=installed/'controller.py'
    source.write_bytes((root/'scripts/local_development_ci.py').read_bytes())
    digest='sha256:'+hashlib.sha256(source.read_bytes()).hexdigest()
    code='''import sys,inspect
sys.path.insert(0,sys.argv[1])
from engineering_hub.development_ci_local import _load_controller
controller=_load_controller(sys.argv[2],sys.argv[3])
assert sys.flags.isolated==1
assert set(('expected_commit','admission_guard','terminal_observer'))<=set(inspect.signature(controller.run).parameters)
print('isolated-installed-controller-load-only')
'''
    result=subprocess.run([sys.executable,'-I','-B','-c',code,str(root/'src'),str(source),digest],
        check=True,capture_output=True,text=True,timeout=15)
    assert result.stdout.strip()=='isolated-installed-controller-load-only'


@pytest.mark.asyncio
@pytest.mark.parametrize('log_failure,expected',[(False,'succeeded'),(True,'failed')])
async def test_owned_queue_through_provider_and_actual_controller_with_fake_docker(
        provider,hooked_controller,tmp_path,log_failure,expected):
    from engineering_hub.development_ci import QueuedDevelopmentCI
    from engineering_hub.pilot_ownership import PilotOwner
    from engineering_hub.repository import HubRepository
    from engineering_hub.work_queue import ClosedClaimPolicy,WorkQueue,WorkAction,WorkTarget
    p,_,_,_,source=provider
    rig=hooked_controller
    rig.state.log_failure=log_failure
    # Real controller code and Linux FD/flock fixtures, but Docker/Git are
    # explicitly synthetic. This is not actual staging execution acceptance.
    p.controller=runner
    p.manifest=DevelopmentCIManifest('staging-pc','expected','sha256:'+'a'*64,
        p.manifest.controller_digest,('sha256:'+hashlib.sha256(b'synthetic lockfile').hexdigest(),)*4,
        profile='python',budget_seconds=10)
    p.engine,p.output=rig.args.engine,rig.output
    del p._docker
    private=tmp_path/'ledger'
    private.mkdir(mode=0o700)
    owner=PilotOwner(private/'hub.sqlite3',allow_ephemeral_test_storage=True)
    try:
        repo=HubRepository(private/'hub.sqlite3',execution_principal='executor:software-validation',pilot_owner=owner)
        queue=WorkQueue(repo,{'development-ci':WorkAction('staging-pc',project='engineering-hub')},
            claim_verifier=ClosedClaimPolicy(),target_verifier=lambda *a:True)
        bridge=QueuedDevelopmentCI(queue,owner,p.manifest,p,action_key='development-ci',renew_interval=.01)
        task=repo.create_task('human:1234','engineering-hub','Local provider fixture','local-provider')
        task=repo.claim_task('codex',task['id'],task['revision'])
        task=repo.set_candidate('codex',task['id'],task['revision'],'c'*40)
        task=repo.review_task('claude',task['id'],task['revision'],'c'*40,'approve')
        job=queue.submit(actor='codex',request_id='one',action_key='development-ci',environment='staging',
            target=WorkTarget(task['id'],'c'*40,p.manifest.digest))
        completed=await bridge.run_once()
        assert completed['id']==job['id'] and completed['state']==expected
        assert rig.events==['create','start','wait'] and rig.state.closed
        with repo._connection() as db:
            assert db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=?',('staging-pc',)).fetchone()['job_id'] is None
        assert len(p._receipts)==1 and await bridge.run_once() is None
    finally:
        owner.close()
