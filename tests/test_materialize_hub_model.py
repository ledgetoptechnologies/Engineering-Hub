import hashlib
import io
import json
from pathlib import Path
import http.server
import subprocess
import sys
import threading
import time

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


def test_supervisor_times_out_before_slow_headers(monkeypatch, tmp_path):
    manifest, _ = _fixture(monkeypatch, tmp_path)
    accepted = threading.Event()
    class SlowHeaders(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            accepted.set()
            time.sleep(1)
        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), SlowHeaders)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # The worker uses a real HTTP connection to a server that accepts the
    # request but does not deliver response headers before the deadline.
    root = Path(__file__).resolve().parents[1]
    code = ("import sys;sys.path.insert(0,sys.argv[1]);"
            "from scripts import materialize_hub_model as m;"
            "m.BASE=sys.argv[2];m.MANIFEST_SHA=sys.argv[3];"
            "m.materialize(m.Path(sys.argv[4]),m.Path(sys.argv[5]))")
    args = [sys.executable, '-I', '-c', code, str(root),
            f'http://127.0.0.1:{server.server_port}/', subject.MANIFEST_SHA,
            str(manifest), str(tmp_path / 'slow-context')]
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            subject.run_bounded(args, timeout=0.4)
        assert accepted.is_set(), 'test must reach a server withholding headers'
        assert time.monotonic() - started < 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_supervisor_does_not_inherit_runner_token(monkeypatch, tmp_path):
    monkeypatch.setenv('GITHUB_TOKEN', 'synthetic-token')
    output = tmp_path / 'environment.json'
    code = "import json,os,sys;open(sys.argv[1],'w').write(json.dumps('GITHUB_TOKEN' in os.environ))"
    subject.run_bounded([sys.executable, '-I', '-c', code, str(output)], timeout=5)
    assert json.loads(output.read_text()) is False
