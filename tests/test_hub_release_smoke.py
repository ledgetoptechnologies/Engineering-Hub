"""Release helper planning tests; actual Docker acceptance runs in Hub CI."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import json
import pytest


def load(monkeypatch):
    scripts = Path(__file__).parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('release_smoke_under_test', scripts / 'hub_release_smoke.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('image,commit', [
    ('busybox:latest', 'a' * 40),
    ('ghcr.io/ledgetoptechnologies/internal-mcp:stable', 'a' * 40),
    ('ghcr.io/ledgetoptechnologies/engineering-hub-control:latest', 'a' * 40),
    ('ghcr.io/ledgetoptechnologies/engineering-hub-control:build-test', 'not-a-sha'),
])
def test_rejects_out_of_scope_artifact_before_docker(monkeypatch, image, commit):
    module = load(monkeypatch)
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: pytest.fail('Docker must not run'))
    with pytest.raises(ValueError):
        module.smoke(image, commit)


def test_isolated_568_profile_and_owned_recreation_plan(monkeypatch):
    module = load(monkeypatch)
    calls = []
    owner = 'b' * 32
    commit = 'a' * 40
    monkeypatch.setattr(module.uuid, 'uuid4', lambda: SimpleNamespace(hex=owner))
    def fake(args, **kwargs):
        calls.append(list(args))
        stdout = ''
        if args[1] == 'inspect' or args[1:3] == ['volume', 'inspect']:
            stdout = owner
        if args[1] == 'exec' and 'image_smoke' in args[-2]:
            stdout = json.dumps({'image_smoke': 'passed', 'build_commit': commit})
        return SimpleNamespace(returncode=0, stdout=stdout, stderr='')
    monkeypatch.setattr(module.subprocess, 'run', fake)
    result = module.smoke('ghcr.io/ledgetoptechnologies/engineering-hub-control:build-test', commit)
    runs = [c for c in calls if c[1] == 'run']
    assert len(runs) == 2
    for c in runs:
        assert c[c.index('--user') + 1] == '568:568'
        assert c[c.index('--network') + 1] == 'none'
        assert c[c.index('--pull') + 1] == 'never'
        assert not {'-p', '--privileged', '--device', '--entrypoint'} & set(c)
        assert 'target=/var/lib/hub' in c[c.index('--mount') + 1]
    probes = [c for c in calls if c[1] == 'exec' and 'image_smoke' in c[-2]]
    assert len(probes) == 2 and all("('Host', 'hub.ledgetoptechnologies.com')" in c[-2] for c in probes)
    assert result['named_volume_recreation'] == 'passed'
    assert result['truenas'] == 'not_deployed'
    assert calls[-1][1:3] == ['volume', 'rm']
