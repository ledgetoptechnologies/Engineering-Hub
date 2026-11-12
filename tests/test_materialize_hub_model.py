import hashlib
import io
import json
from pathlib import Path

import pytest
import yaml

from scripts import materialize_hub_model as subject


def _fixture(monkeypatch, tmp_path):
    names = ('config.json', 'model_optimized.onnx', 'special_tokens_map.json',
             'tokenizer.json', 'tokenizer_config.json')
    values = {name: ('content-' + name).encode() for name in names}
    raw = json.dumps({'schema': 'engineering-hub.model-snapshot.v1',
                      'model': 'BAAI/bge-small-en-v1.5',
                      'files': {k: hashlib.sha256(v).hexdigest() for k, v in values.items()}}).encode()
    manifest = tmp_path / 'manifest.json'
    manifest.write_bytes(raw)
    monkeypatch.setattr(subject, 'MANIFEST_SHA', hashlib.sha256(raw).hexdigest())
    return manifest, values


def test_materializes_only_reviewed_bytes(monkeypatch, tmp_path):
    manifest, values = _fixture(monkeypatch, tmp_path)
    def fetch(request, timeout):
        assert timeout == 20
        assert request.full_url.startswith(subject.BASE)
        assert not request.has_header('Authorization')
        return io.BytesIO(values[request.full_url.rsplit('/', 1)[-1]])
    monkeypatch.setattr(subject, 'urlopen', fetch)
    destination = tmp_path / 'context'
    subject.materialize(manifest, destination)
    assert (destination / 'manifest.json').read_bytes() == manifest.read_bytes()
    assert {p.name: p.read_bytes() for p in (destination / 'model').iterdir()} == values
    with pytest.raises(FileExistsError):
        subject.materialize(manifest, destination)


def test_changed_model_and_manifest_fail_closed(monkeypatch, tmp_path):
    manifest, values = _fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(subject, 'urlopen', lambda request, timeout: io.BytesIO(b'wrong'))
    with pytest.raises(ValueError, match='digest mismatch'):
        subject.materialize(manifest, tmp_path / 'context')
    manifest.write_bytes(b'changed')
    with pytest.raises(ValueError, match='manifest changed'):
        subject.materialize(manifest, tmp_path / 'other')


def test_main_workflow_uses_same_pinned_context_in_both_jobs():
    root = Path(__file__).resolve().parents[1]
    manifest = (root / 'docker/hub-model-manifest.json').read_bytes()
    assert hashlib.sha256(manifest).hexdigest() == subject.MANIFEST_SHA
    workflow = yaml.safe_load((root / '.github/workflows/hub-control.yml').read_text())
    configs = []
    for name in ('verify', 'publish'):
        steps = workflow['jobs'][name]['steps']
        assert any('materialize_hub_model.py' in step.get('run', '') for step in steps)
        builds = [step['with'] for step in steps if step.get('uses', '').startswith('docker/build-push-action@')]
        assert len(builds) == 1
        config = builds[0]
        assert config['file'] == 'docker/Dockerfile.hub-control.pinned'
        assert config['build-contexts'] == 'hub_model=${{ runner.temp }}/hub-model-input'
        configs.append(config['build-args'])
    assert configs[0] == configs[1]
    assert subject.MANIFEST_SHA in configs[0]
