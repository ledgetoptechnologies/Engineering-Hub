"""Thread defaults without loading a model, downloading, or changing host env."""
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_message_bus.semantic import SemanticIndex


def test_real_embedder_constructor_is_single_threaded_and_stays_offline(tmp_path, monkeypatch):
    index = SemanticIndex(tmp_path / 'bus.db', tmp_path / 'files',
                          cache_root=tmp_path / 'search', model_cache_root=tmp_path / 'model')
    monkeypatch.setattr(index, '_probe_model',
                        lambda: ('fixture-hash', 'fixture-version', [tmp_path / 'model.onnx']))
    calls = []
    embedder = object()

    def construct(**kwargs):
        calls.append(kwargs)
        return embedder

    monkeypatch.setitem(sys.modules, 'fastembed', SimpleNamespace(TextEmbedding=construct))
    assert index._ensure_embedder() is embedder
    assert index._ensure_embedder() is embedder
    assert calls == [dict(model_name=index.model_name, cache_dir=str(index.model_cache_root),
                          providers=['CPUExecutionProvider'], threads=1, local_files_only=True)]


def test_injected_embedder_never_probes_or_constructs_a_model(tmp_path, monkeypatch):
    embedder = object()
    index = SemanticIndex(tmp_path / 'bus.db', tmp_path / 'files',
                          cache_root=tmp_path / 'search', embedder=embedder)
    monkeypatch.setattr(index, '_probe_model', lambda: pytest.fail('injected model was probed'))
    monkeypatch.setitem(sys.modules, 'fastembed', SimpleNamespace(
        TextEmbedding=lambda **kwargs: pytest.fail('injected model was replaced')))
    assert index._ensure_embedder() is embedder


def _stage_environment(stage):
    values, pending = {}, ''
    for raw in stage.splitlines():
        line = pending + raw.strip()
        if line.endswith('\\'):
            pending = line[:-1] + ' '
            continue
        pending = ''
        if line.startswith('ENV '):
            values.update(token.split('=', 1) for token in shlex.split(line[4:]))
    return values


def test_core_image_limits_import_pool_before_builder_and_runtime_python():
    recipe = (Path(__file__).resolve().parents[1] / 'docker/Dockerfile.hub-control').read_text()
    # The initial comment also precedes a FROM: header, dashboard, builder,
    # runtime. Keep the stage assertion explicit rather than matching comments.
    _, dashboard, builder, runtime = recipe.split('\nFROM ')
    assert dashboard.startswith('node:')
    assert 'AS builder' in builder
    assert _stage_environment(builder)['OPENBLAS_NUM_THREADS'] == '1'
    assert _stage_environment(runtime)['OPENBLAS_NUM_THREADS'] == '1'
    assert builder.index('OPENBLAS_NUM_THREADS=1') < builder.index('from fastembed import')
    assert 'threads=1' in builder and 'threads=2' not in builder
