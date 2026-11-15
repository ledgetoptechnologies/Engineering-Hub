import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts.verify_hub_build_inputs import read_manifest, verify_inputs


def fixture(tmp_path):
    values = {'model_optimized.onnx': b'model', 'tokenizer.json': b'tokenizer',
              'config.json': b'config', 'vocab.txt': b'vocab'}
    root = tmp_path / 'model'
    root.mkdir()
    for name, value in values.items():
        (root / name).write_bytes(value)
    manifest = json.dumps({'schema': 'engineering-hub.model-snapshot.v1',
                           'model': 'BAAI/bge-small-en-v1.5',
                           'files': {name: hashlib.sha256(value).hexdigest()
                                     for name, value in values.items()}}).encode()
    return dict(node_image='docker.io/library/node@sha256:' + '1'*64,
                manifest=manifest, manifest_sha256=hashlib.sha256(manifest).hexdigest(),
                model_dir=root)


def test_exact_reviewed_inputs_and_reject_mutable_node(tmp_path):
    args = fixture(tmp_path)
    assert verify_inputs(**args)['files'] == 4
    args['node_image'] = 'node:24-bookworm-slim'
    with pytest.raises(ValueError, match='Node'):
        verify_inputs(**args)


@pytest.mark.parametrize('mutation', ['wrong_pin', 'changed', 'extra', 'missing', 'bad_path'])
def test_unreviewed_model_input_is_rejected(tmp_path, mutation):
    args = fixture(tmp_path)
    if mutation == 'wrong_pin':
        args['manifest_sha256'] = '0'*64
    elif mutation == 'changed':
        (args['model_dir'] / 'model_optimized.onnx').write_bytes(b'changed')
    elif mutation == 'extra':
        (args['model_dir'] / 'unexpected').write_bytes(b'extra')
    elif mutation == 'missing':
        (args['model_dir'] / 'tokenizer.json').unlink()
    else:
        value = json.loads(args['manifest'])
        value['files']['../escape'] = '0'*64
        args['manifest'] = json.dumps(value).encode()
        args['manifest_sha256'] = hashlib.sha256(args['manifest']).hexdigest()
    with pytest.raises(ValueError):
        verify_inputs(**args)


def test_symlink_rejected(tmp_path):
    args = fixture(tmp_path)
    original = args['model_dir'] / 'config.json'
    original.unlink()
    try:
        original.symlink_to(args['model_dir'] / 'tokenizer.json')
    except OSError:
        pytest.skip('symlink creation unavailable')
    with pytest.raises(ValueError, match='links'):
        verify_inputs(**args)


def test_walk_errors_do_not_allow_partial_snapshot(tmp_path, monkeypatch):
    args = fixture(tmp_path)
    def broken_walk(root, *, followlinks, onerror):
        onerror(PermissionError('synthetic unreadable subtree'))
        return iter(())
    monkeypatch.setattr(os, 'walk', broken_walk)
    with pytest.raises(ValueError, match='traversal failed'):
        verify_inputs(**args)


def test_manifest_regular_and_bounded(tmp_path):
    path = tmp_path / 'manifest.json'
    path.write_bytes(b'{}')
    assert read_manifest(path) == b'{}'
    path.write_bytes(b'X' * (256 * 1024 + 1))
    with pytest.raises(ValueError, match='regular bounded'):
        read_manifest(path)
    with pytest.raises((ValueError, OSError)):
        read_manifest(tmp_path)


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='FIFO unsupported')
def test_fifo_manifest_rejected_without_blocking(tmp_path):
    path = tmp_path / 'pipe'
    os.mkfifo(path)
    with pytest.raises(ValueError, match='regular bounded'):
        read_manifest(path)
