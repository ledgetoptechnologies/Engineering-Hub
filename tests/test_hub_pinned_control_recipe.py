"""Opt-in recipe contract; these tests do not build images or authenticate inputs."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = 'FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b'


def test_opt_in_recipe_requires_reviewed_pins_and_network_disabled_model_load():
    recipe = (ROOT / 'docker/Dockerfile.hub-control.pinned').read_text()
    assert recipe.index('ARG HUB_NODE_IMAGE') < recipe.index('FROM ${HUB_NODE_IMAGE} AS dashboard')
    assert 'ARG HUB_MODEL_MANIFEST_SHA256' in recipe
    assert 'COPY --from=hub_model / /opt/hub-model-input/' in recipe
    model = recipe.split('COPY --from=input-check /opt/hub-model-input/', 1)[1].split('\nFROM ', 1)[0]
    assert 'RUN --network=none' in model
    assert model.count('python /build/verify_hub_build_inputs.py') == 2
    assert 'HF_HUB_OFFLINE=1' in model and 'local_files_only=True' in model
    assert '/opt/fastembed-cache/bge-small-en-v1.5/' in model
    assert 'curl ' not in model and 'wget ' not in model
    dashboard = recipe.split('FROM ${HUB_NODE_IMAGE} AS dashboard', 1)[1].split('\nFROM ', 1)[0]
    assert dashboard.index('COPY --from=input-check /build/inputs-verified') < dashboard.index('RUN npm ci')


def test_existing_release_recipe_untouched_and_strict_runtime_matches():
    current = (ROOT / 'docker/Dockerfile.hub-control').read_text()
    strict = (ROOT / 'docker/Dockerfile.hub-control.pinned').read_text()
    assert 'FROM node:24-bookworm-slim AS dashboard' in current
    # The runtime image and entrypoint stay byte-for-byte identical. The build
    # has not been run; later runtime smoke must validate installed behavior.
    assert current.rsplit('\n' + BASE + '\n', 1)[1] == strict.rsplit('\n' + BASE + '\n', 1)[1]


def test_fastembed_legacy_offline_layout_is_discoverable(tmp_path, monkeypatch):
    from fastembed.common.model_management import ModelManagement
    from fastembed.text.onnx_embedding import supported_onnx_models

    description = next(item for item in supported_onnx_models
                       if item.model == 'BAAI/bge-small-en-v1.5')
    assert description.model_file == 'model_optimized.onnx'
    assert not description.sources.deprecated_tar_struct
    root = tmp_path / 'cache'
    model = root / 'bge-small-en-v1.5'
    model.mkdir(parents=True)
    for name in ('model_optimized.onnx', 'tokenizer.json', 'config.json'):
        (model / name).write_bytes(b'synthetic-only')

    def unavailable_hf(*args, **kwargs):
        assert kwargs.get('local_files_only') is True
        raise FileNotFoundError('synthetic cache only')
    monkeypatch.setattr(ModelManagement, 'download_files_from_huggingface', unavailable_hf)
    resolved = ModelManagement.download_model(description, str(root), local_files_only=True)
    assert resolved == model
    # The Hub module imports Unix-only fcntl, so execute its unchanged pure
    # snapshot-discovery function in isolation on Windows too. This checks
    # layout only; the actual image must run the installed module and model.
    source = (ROOT / 'src/agent_message_bus/semantic.py').read_text()
    tree = ast.parse(source)
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == '_model_snapshot_files')
    namespace = {'Path': Path, 'MODEL_NAME': description.model}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<snapshot-discovery>', 'exec'), namespace)
    assert {file.name for file in namespace['_model_snapshot_files'](root, description.model)} == {
        'model_optimized.onnx', 'tokenizer.json', 'config.json'}
