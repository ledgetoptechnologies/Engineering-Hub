"""Synthetic harness contracts; no Docker/registry or rollback acceptance claim."""
import importlib.util
import json
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
        if args[:2] == ['image', 'inspect']:
            old = args[-1] == BASE
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
    return smoke.run_smoke(BASE, OLD_COMMIT, NEW, NEW_COMMIT, rollback_mode=mode)


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
    assert 'container_cleanup_unverified' in result['cleanup_errors']


def test_bad_image_label_refuses_before_fixture_creation(monkeypatch):
    docker=Docker();docker.wrong_commit=True
    with pytest.raises(ValueError,match='identity'):run(monkeypatch,docker)
    assert not any(a[0] in ('create','volume') for a,k in docker.calls)


@pytest.mark.parametrize('changes',[{'digests':[]},{'user':'0:0'},{'id':'mutable-name'}])
def test_image_digest_uid_and_id_must_match_before_effect(monkeypatch,changes):
    docker=Docker();docker.image_changes=changes
    with pytest.raises(ValueError):run(monkeypatch,docker)
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
    with pytest.raises(ValueError):smoke.run_smoke(image,commit,NEW,NEW_COMMIT)


def test_incorrect_runtime_image_or_swap_denies_profile(monkeypatch):
    monkeypatch.setattr(smoke.durable,'_profile',lambda *a:None)
    monkeypatch.setattr(smoke,'_docker',lambda *a,**k:subprocess.CompletedProcess([],0,stdout=json.dumps({
        'image':NEW_ID,'swap':-1,'restart':'no','logs':{}})))
    with pytest.raises(ValueError,match='profile'):smoke._profile('x',OLD_ID,{})


def test_workflow_manual_main_read_only_package_scope_and_receipt_only():
    text=(ROOT/'.github/workflows/hub-rollback-verification.yml').read_text()
    assert 'workflow_dispatch:' in text and "github.ref == 'refs/heads/main'" in text
    assert "github.event_name == 'workflow_dispatch'" in text and 'timeout-minutes: 20' in text
    assert 'packages: read' in text and 'contents: read' in text and ': write' not in text
    assert 'docker/login-action@v3' in text and '${{ secrets.GITHUB_TOKEN }}' in text
    assert text.count('timeout 180s docker pull "'+smoke.PACKAGE+'@sha256:')==2
    assert 'path: rollback-receipt.json' in text and 'docker push' not in text and 'internal-mcp' not in text
    assert '${{ inputs.' not in text[text.index('        run: |'):text.index('      - uses: docker/login-action')].split('        run: |',1)[1]
