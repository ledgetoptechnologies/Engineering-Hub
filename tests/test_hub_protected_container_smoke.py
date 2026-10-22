"""Offline release-helper planning; fake Docker and synthetic credentials only."""
import ast
import importlib.util
import json
import os
from pathlib import Path

import pytest

IMAGE='ghcr.io/ledgetoptechnologies/engineering-hub-control:build-protected-test'
COMMIT='a'*40
IMAGE_ID='sha256:'+'b'*64


def load(monkeypatch):
    scripts=Path(__file__).parents[1]/'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec=importlib.util.spec_from_file_location('protected_smoke_test',scripts/'hub_protected_container_smoke.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('image,commit',[(None,COMMIT),(IMAGE,None),(IMAGE,'A'*40),
    ('busybox:latest',COMMIT),(IMAGE+'\n',COMMIT),
    ('ghcr.io/ledgetoptechnologies/engineering-hub-control:latest',COMMIT)])
def test_invalid_artifact_rejected_before_docker(monkeypatch,image,commit):
    module=load(monkeypatch)
    monkeypatch.setattr(module,'run',lambda *a,**k:pytest.fail('Docker must not run'))
    with pytest.raises(ValueError): module.smoke(image,commit)


@pytest.mark.parametrize('failure',[None,'probe','mountpoint','label','network','ambient_remote',
    'ambient_local_remote_context','readonly'])
def test_owned_selective_plan_and_cleanup(monkeypatch,failure):
    module=load(monkeypatch)
    calls,stopped,removed=[],[],[]
    monkeypatch.setenv('CODEX_TOKEN','live-sentinel-never-forward')
    monkeypatch.delenv('DOCKER_HOST',raising=False)
    monkeypatch.setenv('DOCKER_CONTEXT','original-context')
    if failure=='ambient_remote': monkeypatch.setenv('DOCKER_HOST','tcp://remote:2375')
    if failure=='ambient_local_remote_context': monkeypatch.setenv('DOCKER_HOST','unix:///var/run/docker.sock')
    def run(*args,**kw):
        calls.append(args)
        if args[1:3]==('context','inspect'):
            assert os.environ['DOCKER_CONTEXT']=='original-context'
            return json.dumps({'Name':'verified-local','Endpoints':{'docker':{
                'Host':'tcp://remote:2375' if failure in {'network','ambient_local_remote_context'}
                else 'unix:///var/run/docker.sock'}}})
        assert os.environ['DOCKER_CONTEXT']=='verified-local'
        if args[1:3]==('image','inspect'): return IMAGE_ID
        if args[1:3]==('volume','inspect'):
            volume=args[-1]
            owner=volume.removeprefix('hub-protected-state-')
            return json.dumps({'Name':volume,'Labels':{'hub.release-owner':owner if failure!='label' else 'foreign'},
                'Driver':'local','Options':None,
                'Mountpoint':'/var/lib/docker/volumes/'+volume+'/_data' if failure!='mountpoint' else '/live/state'})
        if args[1]=='run' and args[-1]==module.SEED:
            return json.dumps({'protected_seed':'passed','enrollment_cli':'passed',
                               'abort_before_enrollment':'unchanged','reenrollment':'refused'})
        if args[1]=='run' and args[-1]==module.VERIFY: return json.dumps({'preservation':'passed'})
        if args[1:3]==('container','inspect'):
            prior=next(c for c in calls if c[1]=='run' and c[c.index('--name')+1]==args[-1])
            owner=prior[prior.index('--label')+1].split('=',1)[1]
            mounts=[]
            for i,value in enumerate(prior):
                if value!='--mount': continue
                fields=dict(part.split('=',1) for part in prior[i+1].split(',') if '=' in part)
                mounts.append({'Type':'bind','Source':fields['source'],'Destination':fields['target'],
                               'RW':not prior[i+1].endswith(',readonly')})
            if failure=='readonly': mounts[0]['RW']=True
            return json.dumps({'Image':IMAGE_ID,'Config':{'User':'568:568','Labels':{'hub.release-owner':owner}},
                'HostConfig':{'NetworkMode':'none','PortBindings':{},'ReadonlyRootfs':True,'Privileged':False,'Binds':[],
                    'Memory':512*1024*1024,'MemorySwap':512*1024*1024,'NanoCpus':1_000_000_000,
                    'PidsLimit':128,'CapDrop':['ALL'],'SecurityOpt':['no-new-privileges:true']},
                'Mounts':mounts})
        if args[1]=='exec':
            if failure=='probe': raise RuntimeError('probe failed')
            return json.dumps({'settings_profile':'passed','build_commit':COMMIT})
        if args[1]=='wait': return '0'
        return ''
    monkeypatch.setattr(module,'run',run)
    def stop(*args):
        assert os.environ['DOCKER_CONTEXT']=='verified-local'
        stopped.append(args)
    def remove(*args):
        assert os.environ['DOCKER_CONTEXT']=='verified-local'
        removed.append(args)
    monkeypatch.setattr(module,'stop_owned',stop)
    monkeypatch.setattr(module,'remove_owned_volume',remove)
    if failure:
        with pytest.raises((RuntimeError,ValueError,AssertionError)): module.smoke(IMAGE,COMMIT)
    else:
        result=module.smoke(IMAGE,COMMIT)
        assert result['recreations']==2 and len(result['stages'])==3
        assert result['runtime']=='inactive' and result['selective_maps']==3
        assert result['enrollment_cli']['abort_before_enrollment']=='unchanged'
        assert result['enrollment_cli']['reenrollment']=='refused'
    runs=[c for c in calls if c[1]=='run']
    for args in runs:
        assert args[args.index('--pull')+1]=='never'
        assert args[args.index('--network')+1]=='none'
        assert args[args.index('--memory')+1]==args[args.index('--memory-swap')+1]=='512m'
        assert args[args.index('--cpus')+1]=='1' and args[args.index('--pids-limit')+1]=='128'
        assert args[args.index('--user')+1]=='568:568'
        assert '--read-only' in args and args[args.index('--cap-drop')+1]=='ALL'
        assert not {'--privileged','--publish','-p','--device','--env-file'} & set(args)
        assert not any('live-sentinel' in value for value in args)
        if '-d' in args:
            assert args[-8:]==('python',IMAGE_ID,'-I','-B','-m','engineering_hub.owned_inactive_hub','--config','/var/lib/hub/settings.json')
            maps=[args[i+1] for i,value in enumerate(args) if value=='--mount']
            assert len(maps)==3 and all(value.startswith('type=bind,source=/var/lib/docker/volumes/hub-protected-state-') for value in maps)
            assert any(value.endswith('target=/var/lib/hub/settings.json,readonly') for value in maps)
            assert not any(value.endswith('target=/var/lib/hub') for value in maps)
            env=[args[i+1] for i,value in enumerate(args) if value=='-e']
            assert len(env)==4 and all('_TOKEN=synthetic-protected-' in value for value in env)
            assert not any('EXECUTOR' in value or 'RUNTIME' in value for value in env)
    assert len(stopped)==len(runs)
    assert len(removed)==(0 if failure in {'network','ambient_remote','ambient_local_remote_context'} else 1)
    assert os.environ['DOCKER_CONTEXT']=='original-context'
    if failure in {'ambient_remote','ambient_local_remote_context'}:
        assert calls==[]


def test_embedded_scripts_compile_and_keep_real_guards(monkeypatch):
    module=load(monkeypatch)
    for script in (module.SEED,module.PROBE,module.VERIFY): ast.parse(script)
    assert 'allow_ephemeral' not in module.SEED
    assert 'INSERT INTO hub_pilot_owner' not in module.SEED
    assert "'engineering_hub.pilot_enrollment',*args]" in module.SEED
    assert "enrollment_cli('inspect'" in module.SEED
    assert 'document[\'plan\'][\'snapshot\']' in module.SEED
    assert 'aborted.returncode == 1' in module.SEED and 'not lock.exists()' in module.SEED
    assert 'retry.returncode == 1' in module.SEED and 'lock.read_bytes() == lock_bytes' in module.SEED
    assert "overview['autonomy'] ==" in module.PROBE
    assert "assert 8765 not in listeners" in module.PROBE


@pytest.mark.parametrize('source',['missing marker','one anchor and another anchor'])
def test_changed_probe_anchor_refuses_silent_skip(monkeypatch,source):
    module=load(monkeypatch)
    with pytest.raises(ValueError,match='contract anchor changed'):
        module._replace_once(source,'anchor','protected assertions')


def test_exact_single_probe_anchor_replacement(monkeypatch):
    module=load(monkeypatch)
    assert module._replace_once('before TARGET after','TARGET','checked')=='before checked after'
