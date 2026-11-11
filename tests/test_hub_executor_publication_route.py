"""Source contract of the opt-in manual route; no Actions dispatch or registry."""
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
FIELDS = ('source_commit', 'core_digest', 'core_commit', 'core_config',
          'wheel_sha256', 'source_date_epoch')


def workflow(name):
    return (ROOT / '.github/workflows' / name).read_text()


def test_all_executor_and_core_python_inputs_use_same_immutable_base():
    core = (ROOT / 'docker/Dockerfile.hub-control').read_text()
    executor = (ROOT / 'docker/Dockerfile.hub-executor').read_text()
    pattern = r'python:3\.12-slim@sha256:([0-9a-f]{64})'
    core_pins = re.findall(r'^FROM ' + pattern + r'(?: AS builder)?$', core, re.M)
    executor_pins = re.findall(r'^FROM ' + pattern + r' AS python-runtime$', executor, re.M)
    assert len(core_pins) == 2 and len(executor_pins) == 1
    assert core_pins[0] == core_pins[1] == executor_pins[0]
    for name in ('hub-executor-compatibility.yml', 'hub-executor-publication.yml'):
        assert re.findall(pattern, workflow(name)) == executor_pins


def test_default_route_remains_readonly_and_manual_main_only():
    text = workflow('hub-executor-compatibility.yml')
    triggers, jobs = text.split('jobs:\n', 1)
    assert re.findall(r'^  ([a-z_]+):$', triggers, re.M) == ['workflow_dispatch']
    assert ('      publish_staging:\n'
            '        description: Explicitly publish the reviewed staging executor (default stays read-only)\n'
            '        type: boolean\n        required: false\n        default: false\n') in triggers
    assert triggers.endswith('permissions:\n  contents: read\n  packages: read\n')
    readonly, publication = jobs.split('  publication:\n', 1)
    assert "if: ${{ !inputs.publish_staging && github.ref == 'refs/heads/main' && github.event_name == 'workflow_dispatch' }}" in readonly
    assert 'packages: write' not in readonly
    for mode in ('validate', 'wheel', 'compatibility'):
        assert 'run: python -B scripts/hub_executor_compatibility_ci.py ' + mode in readonly
    assert 'ref: ${{ github.sha }}' in readonly and 'persist-credentials: false' in readonly
    assert 'executor-compatibility-public-receipt' in readonly
    assert "if: ${{ inputs.publish_staging && github.ref == 'refs/heads/main' && github.event_name == 'workflow_dispatch' }}" in publication


def test_optin_uses_only_fixed_local_callee_with_same_six_inputs():
    publication = workflow('hub-executor-compatibility.yml').split('  publication:\n', 1)[1]
    assert '    permissions:\n      contents: read\n      packages: write\n' in publication
    assert '    uses: ./.github/workflows/hub-executor-publication.yml\n' in publication
    assert 'steps:' not in publication and 'secrets:' not in publication
    mappings = re.findall(r'^      ([a-z_0-9]+): \$\{\{ inputs\.([a-z_0-9]+) \}\}$', publication, re.M)
    assert mappings == [(field, field) for field in FIELDS]
    assert len(publication.split('    uses: ')) == 2


def test_child_only_adds_required_typed_local_call_and_retains_guards():
    text = workflow('hub-executor-publication.yml')
    call = text.split('  workflow_call:\n', 1)[1].split('  workflow_dispatch:\n', 1)[0]
    assert re.findall(r'^      ([a-z_0-9]+):$', call, re.M) == list(FIELDS)
    for field in FIELDS:
        assert '      ' + field + ':\n        type: string\n        required: true\n' in call
    events = text.split('permissions:\n', 1)[0]
    assert re.findall(r'^  ([a-z_]+):$', events, re.M) == ['workflow_call', 'workflow_dispatch']
    assert "if: ${{ github.ref == 'refs/heads/main' && github.event_name == 'workflow_dispatch' }}" in text
    assert 'run: python -B scripts/hub_executor_compatibility_ci.py validate' in text
    assert 'run: python -B scripts/publish_hub_executor.py' in text
    assert 'ref: ${{ github.sha }}' in text and 'persist-credentials: false' in text
