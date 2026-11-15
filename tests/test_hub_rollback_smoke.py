"""Synthetic harness contracts; no Docker/registry or rollback acceptance claim."""
import importlib.util
from contextlib import contextmanager
import json
import re
from pathlib import Path
import subprocess
import itertools
from types import SimpleNamespace
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rollback_smoke', ROOT / 'scripts/hub_rollback_container_smoke.py')
smoke = importlib.util.module_from_spec(spec); spec.loader.exec_module(smoke)
spec = importlib.util.spec_from_file_location('durable_test_helpers', ROOT / 'tests/test_hub_durable_smoke.py')
helpers = importlib.util.module_from_spec(spec); spec.loader.exec_module(helpers)
BASE, NEW = smoke.PACKAGE + '@sha256:' + 'a'*64, smoke.PACKAGE + '@sha256:' + 'b'*64
OLD_COMMIT, NEW_COMMIT = '1'*40, '2'*40
OLD_ID, NEW_ID = 'sha256:'+'c'*64, 'sha256:'+'d'*64


@pytest.fixture(autouse=True)
def mocked_session(monkeypatch):
    @contextmanager
    def session(host):
        assert host == 'unix:///run/test/docker.sock'
        monkeypatch.setattr(smoke.durable, '_docker', lambda *a, **k: smoke._docker(*a, **k))
        yield (('/usr/bin/docker', '--host', host), {'PATH': '/usr/bin:/bin', 'DOCKER_CONFIG': '/test'})
    monkeypatch.setattr(smoke.durable, 'docker_session', session)


class Docker(helpers.Docker):
    def __init__(self):
        super().__init__()
        self.initial, self.written = str(uuid.uuid4()), str(uuid.uuid4())
        self.wrong_commit = False
        self.stop_fails = False
        self.write_fails = False
        self.image_for = {}
        self.running = {}
        self.terminal = {}
        self.image_changes = {}
        self.helper_state = None

    def __call__(self, args, **kwargs):
        out = None
        if (args[:2] == ['volume','inspect'] and args[-1] not in self.volumes):
            self.calls.append((args,kwargs))
            return subprocess.CompletedProcess(args,1,stdout='',stderr='Error response from daemon: get '+args[-1]+': no such volume\n')
        if (args[:2] == ['container','inspect'] and args[-1] not in self.containers and args[-1] not in self.profiles):
            self.calls.append((args,kwargs))
            return subprocess.CompletedProcess(args,1,stdout='',stderr='Error response from daemon: No such container: '+args[-1]+'\n')
        if args[:2] == ['image', 'inspect']:
            old = args[-1] in (BASE, OLD_ID)
            out = json.dumps(dict({'id': OLD_ID if old else NEW_ID, 'revision': 'f'*40 if self.wrong_commit else
                OLD_COMMIT if old else NEW_COMMIT, 'user': '568:568', 'digests': [args[-1]]},**self.image_changes))
        elif args[0] == 'create':
            result = super().__call__(args, **kwargs)
            identity = self.containers[args[args.index('--name')+1]][0]
            self.image_for[identity] = OLD_ID if OLD_ID in args else NEW_ID
            self.running[identity] = False
            if smoke.durable.RESTORE in args:
                self.helper = identity
            return result
        elif args[:2] == ['container', 'inspect'] and '"image"' in args[-2]:
            out = json.dumps({'image': self.image_for[args[-1]], 'swap': 512*1024**2, 'restart': 'no',
                'logs': {'Type':'json-file','Config':{'max-file':'1','max-size':'128k'}}})
        elif args[:2] == ['container', 'inspect'] and '"running"' in args[-2]:
            state={'running':self.running[args[-1]],'pid':100 if self.running[args[-1]] else 0,'oom':False,'exit':0}
            if args[-1]==getattr(self,'helper',None) and self.helper_state is not None:
                state=self.helper_state
            out = json.dumps(state)
            if state['running'] is False and state['pid']==0 and state['exit']==0 and state['oom'] is False:
                self.terminal.setdefault(args[-1],len(self.calls))
        elif args[0] == 'start' and args[1] != '--attach':
            self.running[args[-1]]=True
            out=args[-1]
        elif args[:3] == ['kill','--signal','SIGTERM']:
            if self.stop_fails:
                self.calls.append((args, kwargs))
                raise TimeoutError('uncertain TERM')
            self.running[args[-1]]=False
            out = args[-1]
        elif args[0] == 'exec' and ('mode, commit' in args[4]):
            mode, commit = args[5:7]
            task_id = self.written if 'candidate-write-fixture' in args[4] else self.initial
            if self.write_fails and 'candidate-write-fixture' in args[4]:
                task_id = self.initial
            if mode == 'verify':
                task_id = args[7]
            out = json.dumps({'task_id':task_id,'revision':2,'state':'claimed','build_commit':commit,'auth_denials':True})
        elif args[0] == 'exec' and 'original-backup-only' in args[4]:
            out = 'original-backup-only'
        elif args[0] == 'rm':
            assert '--force' not in args
            self.calls.append((args, kwargs))
            self.containers = {k:v for k,v in self.containers.items() if v[0] != args[-1]}
            return subprocess.CompletedProcess(args,0,stdout='',stderr='')
        if out is None:
            return super().__call__(args, **kwargs)
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args,0,stdout=out,stderr='')


def run(monkeypatch, docker, mode='direct'):
    monkeypatch.setattr(smoke, '_docker', docker)
    return smoke.run_smoke(BASE, OLD_COMMIT, NEW, NEW_COMMIT, rollback_mode=mode, docker_host='unix:///run/test/docker.sock')


def test_direct_candidate_write_survives_old_code_with_normal_exclusive_handoff(monkeypatch):
    docker = Docker(); result = run(monkeypatch,docker)
    assert result['state']=='passed' and result['cleanup']=='completed'
    assert result['candidate_state_after_rollback']=='matched'
    calls=[a for a,k in docker.calls]
    creates=[(i,a) for i,a in enumerate(calls) if a[0]=='create']
    assert [a[-1] for _,a in creates]==[OLD_ID,NEW_ID,OLD_ID]
    for (index,_),old in zip(creates[1:],[f'{1:064x}',f'{2:064x}']):
        assert docker.terminal[old]<index
    assert not docker.containers and not docker.volumes
    assert not any('--force' in a or '--publish' in a or '--privileged' in a for a in calls)
    for _,a in creates:
        assert a[a.index('--network')+1]=='none' and a[a.index('--user')+1]=='568:568'
        assert a[a.index('--memory-swap')+1]=='512m' and '--pull' in a
    old_probes=[a for a in calls if a[0]=='exec' and len(a)>7 and a[6]==OLD_COMMIT]
    assert {a[7] for a in old_probes}=={docker.initial,docker.written}


def test_backup_mode_proves_original_backup_not_candidate_preservation(monkeypatch):
    docker=Docker();result=run(monkeypatch,docker,'backup-restore')
    assert result['state']=='passed' and result['candidate_state_after_rollback']=='intentionally_discarded'
    calls=[a for a,k in docker.calls]
    copy=next(a for a in calls if a[0]=='create' and smoke.durable.RESTORE in a)
    assert any('target=/backup-source,readonly' in x for x in copy)
    assert any(a[0]=='exec' and 'original-backup-only' in a[4] for a in calls)
    assert not docker.volumes and not docker.containers


def test_no_distinct_candidate_write_never_reports_success(monkeypatch):
    docker=Docker();docker.write_fails=True
    result=run(monkeypatch,docker)
    assert result['state']=='failed' and result['cleanup']=='completed'
    assert len([a for a,k in docker.calls if a[0]=='create'])==2


def test_unknown_stop_denies_second_writer_and_retains_owned_resources(monkeypatch):
    clock=itertools.count()
    monkeypatch.setattr(smoke,'time',SimpleNamespace(monotonic=lambda:next(clock),sleep=lambda _:None))
    docker=Docker();docker.stop_fails=True
    result=run(monkeypatch,docker)
    assert result['state']=='failed' and result['cleanup']=='held'
    assert len([a for a,k in docker.calls if a[0]=='create'])==1
    assert not any(a[0]=='rm' for a,k in docker.calls)
    assert not any(a[:2]==['volume','rm'] for a,k in docker.calls)
    assert len([a for a,k in docker.calls if a[0]=='kill'])==1
    assert any(x['kind']=='container_cleanup_unverified' for x in result['cleanup_errors'])
    assert result['failure_phase']=='baseline.terminal' and result['error_class']=='timeout'


def test_bad_image_label_refuses_before_fixture_creation(monkeypatch):
    docker=Docker();docker.wrong_commit=True
    result=run(monkeypatch,docker)
    assert result['state']=='failed' and result['failure_phase']=='baseline.image'
    assert not any(a[0] in ('create','volume') for a,k in docker.calls)


@pytest.mark.parametrize('changes',[{'digests':[]},{'user':'0:0'},{'id':'mutable-name'}])
def test_image_digest_uid_and_id_must_match_before_effect(monkeypatch,changes):
    docker=Docker();docker.image_changes=changes
    result=run(monkeypatch,docker)
    assert result['state']=='failed' and result['failure_phase']=='baseline.image'
    assert not any(a[0] in ('create','volume') for a,k in docker.calls)


@pytest.mark.parametrize('state',[
    {'running':True,'pid':100,'oom':False,'exit':0},
    {'running':False,'pid':0,'oom':True,'exit':0},
    {'running':False,'pid':0,'oom':False,'exit':1},
    {'running':False,'pid':100,'oom':False,'exit':0},
])
def test_restore_helper_unknown_or_failed_terminal_denies_old_writer(monkeypatch,state):
    clock=itertools.count()
    monkeypatch.setattr(smoke,'time',SimpleNamespace(monotonic=lambda:next(clock),sleep=lambda _:None))
    docker=Docker();docker.helper_state=state
    result=run(monkeypatch,docker,'backup-restore')
    assert result['state']=='failed'
    assert len([a for a,k in docker.calls if a[0]=='create'])==3


@pytest.mark.parametrize('image,commit',[
    ('ghcr.io/other/image@sha256:'+'a'*64,OLD_COMMIT),(smoke.PACKAGE+':latest',OLD_COMMIT),
    (BASE+';echo nope',OLD_COMMIT),(BASE,'main'),(BASE,'A'*40),
])
def test_input_validation_precedes_any_cli(monkeypatch,image,commit):
    cli=lambda *a,**k:pytest.fail('unexpected CLI')
    monkeypatch.setattr(smoke,'_docker',cli)
    with pytest.raises(ValueError):smoke.run_smoke(image,commit,NEW,NEW_COMMIT, docker_host='unix:///run/test/docker.sock')


def test_incorrect_runtime_image_or_swap_denies_profile(monkeypatch):
    monkeypatch.setattr(smoke.durable,'_profile',lambda *a:None)
    monkeypatch.setattr(smoke,'_docker',lambda *a,**k:subprocess.CompletedProcess([],0,stdout=json.dumps({
        'image':NEW_ID,'swap':-1,'restart':'no','logs':{}})))
    with pytest.raises(ValueError,match='profile'):smoke._profile('x',OLD_ID,{})


def workflow_run_commands(text):
    """Extract this workflow's block/inline run forms; this is not a YAML parser."""
    lines = text.splitlines()
    commands = []
    i = 0
    while i < len(lines):
        match = re.match(r'^(\s*)(?:-\s+)?run:\s*(.*)$', lines[i])
        if not match:
            i += 1
            continue
        indent, value = len(match.group(1)), match.group(2)
        if value in ('|', '|-', '|+', '>', '>-', '>+'):
            body = []
            i += 1
            while i < len(lines):
                line = lines[i]
                if line.strip() and len(line) - len(line.lstrip()) <= indent:
                    break
                body.append(line)
                i += 1
            commands.append('\n'.join(body))
        else:
            commands.append(value)
            i += 1
    return commands


def has_direct_input_expression(command):
    return bool(re.search(r'\$\{\{(?:(?!\}\}).)*?\binputs\b',
                          command, re.IGNORECASE | re.DOTALL))


@pytest.mark.parametrize('command', [
    'echo ${{ inputs.baseline_digest }}',
    'echo ${{inputs.baseline_digest}}',
    'echo ${{  inputs  .baseline_digest }}',
    'echo ${{ INPUTS . baseline_digest }}',
    "echo ${{ inputs ['baseline_digest'] }}",
    'echo ${{ format("{0}", inputs.baseline_digest) }}',
    'echo ${{ toJSON(inputs) }}',
    'echo ${{ inputs }}',
])
@pytest.mark.parametrize('run', ['        run: |\n          {}\n', '        run: {}\n',
                                '      - run: {}\n'])
def test_direct_input_interpolation_is_rejected_for_block_and_inline(run, command):
    commands = workflow_run_commands(run.format(command))
    assert len(commands) == 1 and has_direct_input_expression(commands[0])


def test_shell_environment_reference_is_not_direct_interpolation():
    assert not has_direct_input_expression('echo "$BASELINE_DIGEST"')


def test_workflow_manual_main_read_only_package_scope_and_receipt_only():
    text=(ROOT/'.github/workflows/hub-rollback-verification.yml').read_text()
    assert 'workflow_dispatch:' in text and "github.ref == 'refs/heads/main'" in text
    assert "github.event_name == 'workflow_dispatch'" in text and 'timeout-minutes: 20' in text
    assert 'packages: read' in text and 'contents: read' in text and ': write' not in text
    assert 'docker/login-action@v3' in text and '${{ secrets.GITHUB_TOKEN }}' in text
    pulls = [line.strip() for line in text.splitlines() if ' docker ' in line and ' pull ' in line]
    assert pulls == [
        'env -u DOCKER_CONTEXT timeout 180s docker --host "$ENGINE_ENDPOINT" pull "'
        + smoke.PACKAGE + '@sha256:$BASELINE_DIGEST"',
        'env -u DOCKER_CONTEXT timeout 180s docker --host "$ENGINE_ENDPOINT" pull "'
        + smoke.PACKAGE + '@sha256:$CANDIDATE_DIGEST"',
    ]
    assert text.count('ENGINE_ENDPOINT: ${{ steps.engine.outputs.sock }}') == 2
    assert 'test -n "$ENGINE_ENDPOINT"' in text
    assert '--docker-host "$ENGINE_ENDPOINT"' in text
    assert '[[ "$BASELINE_DIGEST" =~ ^[a-f0-9]{64}$ ]]' in text
    assert '[[ "$CANDIDATE_DIGEST" =~ ^[a-f0-9]{64}$ ]]' in text
    assert 'path: rollback-receipt.json' in text and 'docker push' not in text and 'internal-mcp' not in text
    commands = workflow_run_commands(text)
    assert len(commands) == 4
    assert all(not has_direct_input_expression(command) for command in commands)


@pytest.mark.parametrize('kind', ['volume','container'])
def test_scoped_missing_daemon_response_is_allowed_only_for_absence_probe(monkeypatch,kind):
    name='hub-rollback-'+'a'*32+('-state' if kind=='volume' else '-baseline')
    error=('Error response from daemon: get '+name+': no such volume' if kind=='volume' else
           'Error response from daemon: No such container: '+name)
    monkeypatch.setattr(smoke,'_docker',lambda *a,**k:subprocess.CompletedProcess([],1,stdout='',stderr=error+'\n'))
    function=smoke._volume if kind=='volume' else smoke._container
    assert function(name,'owner',missing_ok=True) is (False if kind=='volume' else None)
    with pytest.raises(smoke.PublicFailure):function(name,'owner')


@pytest.mark.parametrize('kind', ['volume','container'])
@pytest.mark.parametrize('change', ['foreign','permission','prefix','suffix','stdout','code','unscoped'])
def test_ambiguous_permission_or_foreign_missing_never_allows_creation(monkeypatch,kind,change):
    name='hub-rollback-'+'a'*32+('-state' if kind=='volume' else '-baseline')
    error=('Error response from daemon: get '+name+': no such volume' if kind=='volume' else
           'Error response from daemon: No such container: '+name)
    raw={'foreign':error.replace(name,name+'-foreign'),'permission':'permission denied: '+error,
         'prefix':'daemon unavailable\n'+error,'suffix':error+'\npermission denied',
         'unscoped':'No such '+kind}.get(change,error)
    result=subprocess.CompletedProcess([],2 if change=='code' else 1,stdout='partial' if change=='stdout' else '',stderr=raw)
    monkeypatch.setattr(smoke,'_docker',lambda *a,**k:result)
    function=smoke._volume if kind=='volume' else smoke._container
    with pytest.raises(smoke.PublicFailure):function(name,'owner',missing_ok=True)


def test_first_fixture_failure_has_safe_phase_code_and_no_private_error(monkeypatch):
    docker=Docker()
    def failure(args,**kwargs):
        if args[:2]==['volume','inspect']:
            return subprocess.CompletedProcess(args,1,stdout='',stderr='permission denied SECRET_DO_NOT_PUBLISH')
        return docker(args,**kwargs)
    monkeypatch.setattr(smoke,'_docker',failure)
    result=smoke.run_smoke(BASE,OLD_COMMIT,NEW,NEW_COMMIT, docker_host='unix:///run/test/docker.sock')
    assert result['failure_phase']=='fixture.volume0.absence'
    assert result['entered_steps']==['baseline.image','candidate.image','fixture.volume0.absence']
    assert result['completed_steps']==['baseline.image','candidate.image']
    assert result['error_code']=='volume_inspection_unverified' and result['cli_exit_code']==1
    assert result['cleanup']=='completed' and 'SECRET' not in json.dumps(result)
    assert not any(a[0] in ('create','volume') for a,k in docker.calls)


def test_missing_inspect_timeout_remains_unknown(monkeypatch):
    def timeout(*a,**k):raise TimeoutError('private socket path')
    monkeypatch.setattr(smoke,'_docker',timeout)
    with pytest.raises(TimeoutError):smoke._volume('hub-rollback-'+'a'*32+'-state','owner',missing_ok=True)
    assert smoke._failure(TimeoutError('private socket path'))=={'error_class':'timeout','error_code':'timeout'}


def test_local_image_requires_opt_in_and_exact_id(monkeypatch):
    with pytest.raises(ValueError):
        smoke.validate(OLD_ID, OLD_COMMIT)
    docker = Docker()
    monkeypatch.setattr(smoke, '_docker', docker)
    docker.image_changes = {'id': OLD_ID, 'revision': OLD_COMMIT, 'digests': None}
    assert smoke._image(OLD_ID, OLD_COMMIT, local_images=True) == OLD_ID
    docker.image_changes['id'] = NEW_ID
    with pytest.raises(ValueError):
        smoke._image(OLD_ID, OLD_COMMIT, local_images=True)
    docker.image_changes.update(id=OLD_ID, user='0:0')
    with pytest.raises(ValueError):
        smoke._image(OLD_ID, OLD_COMMIT, local_images=True)
    docker.image_changes.update(user='568:568', revision=NEW_COMMIT)
    with pytest.raises(ValueError):
        smoke._image(OLD_ID, OLD_COMMIT, local_images=True)


def test_docker_requires_session_and_passes_bound_prefix_and_environment(monkeypatch):
    with pytest.raises(RuntimeError, match='session'):
        smoke._docker(['info'])
    calls = []
    class Child:
        returncode = 0
        def poll(self): return 0
        def wait(self): return 0
    monkeypatch.setattr(smoke.subprocess, 'Popen', lambda argv, **kw: calls.append((argv, kw)) or Child())
    token = smoke._session.set((('/usr/bin/docker', '--host', 'unix:///run/test/docker.sock'), {'PATH': '/usr/bin:/bin'}))
    try:
        smoke._docker(['info'])
    finally:
        smoke._session.reset(token)
    assert calls[0][0] == ['/usr/bin/docker', '--host', 'unix:///run/test/docker.sock', 'info']
    assert calls[0][1]['env'] == {'PATH': '/usr/bin:/bin'}


def test_local_images_complete_with_distinct_ids_and_cleanup(monkeypatch):
    docker = Docker()
    monkeypatch.setattr(smoke, '_docker', docker)
    result = smoke.run_smoke(OLD_ID, OLD_COMMIT, NEW_ID, NEW_COMMIT,
        docker_host='unix:///run/test/docker.sock', local_images=True)
    assert result['state'] == 'passed' and result['cleanup'] == 'completed'
    assert not docker.containers and not docker.volumes
    with pytest.raises(ValueError):
        smoke.run_smoke(OLD_ID, OLD_COMMIT, OLD_ID, NEW_COMMIT,
            docker_host='unix:///run/test/docker.sock', local_images=True)
