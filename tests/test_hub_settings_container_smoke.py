"""Offline helper planning checks; no Docker calls in this unit suite."""
import ast
import importlib.util
import json
from pathlib import Path

import pytest


IMAGE = 'ghcr.io/ledgetoptechnologies/engineering-hub-control:build-settings-test'
COMMIT = 'a' * 40
IMAGE_ID = 'sha256:' + 'b' * 64


def load(monkeypatch):
    scripts = Path(__file__).parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('settings_smoke_under_test', scripts / 'hub_settings_container_smoke.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('image,commit', [
    ('busybox:latest',COMMIT), ('ghcr.io/ledgetoptechnologies/engineering-hub-control:latest',COMMIT),
    (IMAGE,'A'*40), (IMAGE,'not-sha'), (None,COMMIT), (IMAGE,None),
    (IMAGE+'\n',COMMIT), ('ghcr.io/ledgetoptechnologies/engineering-hub-control@sha256:'+'g'*64,COMMIT),
])
def test_invalid_artifact_never_calls_docker(monkeypatch,image,commit):
    module = load(monkeypatch)
    monkeypatch.setattr(module,'run',lambda *a,**kw: pytest.fail('Unexpected Docker operation'))
    with pytest.raises(ValueError):
        module.smoke(image,commit)


@pytest.mark.parametrize('fail_probe',[False,True,'missing_note','wrong_version'])
def test_owned_plan_uses_packaged_settings_only_and_cleans_up(monkeypatch,fail_probe):
    module = load(monkeypatch)
    calls, stopped, volumes = [], [], []
    monkeypatch.setenv('CODEX_TOKEN','live-sentinel-do-not-forward')
    def run(*args,**kwargs):
        calls.append(args)
        if args[1:3] == ('image','inspect'):
            return IMAGE_ID
        if args[1] == 'run' and args[-1] == module.SEED:
            return json.dumps({'seed':'passed'})
        if args[1:3] == ('container','inspect'):
            prior = next(call for call in calls if call[1] == 'run' and call[call.index('--name')+1] == args[-1])
            owner = prior[prior.index('--label')+1].split('=',1)[1]
            volume = prior[prior.index('--mount')+1].split('source=',1)[1].split(',')[0]
            return json.dumps({'Config':{'User':'568:568','Labels':{'hub.release-owner':owner}},
                'HostConfig':{'NetworkMode':'none','PortBindings':{},'Binds':[],
                              'Privileged':False,'ReadonlyRootfs':True},
                'Mounts':[{'Type':'volume','Name':volume,'Destination':'/var/lib/hub'}]})
        if args[1] == 'exec':
            if fail_probe is True:
                raise RuntimeError('Synthetic probe failure')
            generation = sum(call[1] == 'exec' for call in calls)
            report = {'settings_profile':'passed','build_commit':COMMIT,
                      'preserved_note':'passed','notes_authenticated_agents':3,
                      'note_version':1+3*generation}
            if fail_probe == 'missing_note':
                report.pop('preserved_note')
            elif fail_probe == 'wrong_version':
                report['note_version'] = 1
            return json.dumps(report)
        return ''
    monkeypatch.setattr(module,'run',run)
    monkeypatch.setattr(module,'stop_owned',lambda name,owner: stopped.append((name,owner)))
    monkeypatch.setattr(module,'remove_owned_volume',lambda name,owner: volumes.append((name,owner)))
    if fail_probe is True:
        with pytest.raises(RuntimeError,match='Synthetic probe failure'):
            module.smoke(IMAGE,COMMIT)
    elif fail_probe:
        with pytest.raises((KeyError, AssertionError)):
            module.smoke(IMAGE,COMMIT)
    else:
        report = module.smoke(IMAGE,COMMIT)
        assert report['recreations'] == 2 and len(report['stages']) == 3
        assert report['network'] == 'none' and report['truenas'] == 'not_deployed'
        assert report['host_mounts'] is report['host_ports'] is False
        assert [stage['note_version'] for stage in report['stages']] == [4,7,10]
        assert all(stage['notes_authenticated_agents'] == 3 for stage in report['stages'])
    runs = [args for args in calls if args[1] == 'run']
    assert len(runs) == (2 if fail_probe else 4)
    for args in runs:
        assert args[args.index('--network')+1] == 'none'
        assert args[args.index('--pull')+1] == 'never'
        assert args[args.index('--user')+1] == '568:568'
        assert args[args.index('--mount')+1].startswith('type=volume,source=hub-settings-state-')
        assert '--read-only' in args and '--cap-drop' in args
        assert not {'-p','--publish','--privileged','--device','--env-file'} & set(args)
        assert not any('live-sentinel' in value for value in args)
    assert '--entrypoint' in runs[0] and runs[0][-3:] == (IMAGE_ID,'-c',module.SEED)
    for args in runs[1:]:
        assert '--entrypoint' not in args and args[-3:] == (IMAGE_ID,'--config','/var/lib/hub/settings.json')
        env = [args[i+1] for i,value in enumerate(args) if value == '-e']
        assert len(env) == 4 and all('_TOKEN=synthetic-settings-' in value for value in env)
    assert len(stopped) == len(runs) and len(volumes) == 1
    owner = volumes[0][1]
    assert all(item[1] == owner for item in stopped)


def test_embedded_probes_are_valid_python(monkeypatch):
    module = load(monkeypatch)
    ast.parse(module.SEED)
    ast.parse(module.PROBE)
