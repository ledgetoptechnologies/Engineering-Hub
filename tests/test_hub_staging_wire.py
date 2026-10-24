"""Keep consumer fixtures byte-for-byte aligned with real producer responses."""
import importlib.util
import json
from pathlib import Path
import uuid


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('export_staging_wire_fixture',
                                            ROOT / 'scripts/export_staging_wire.py')
EXPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPORT)


def test_tracked_wire_fixture_is_exact_real_api_export():
    assert EXPORT.FIXTURE.read_bytes() == EXPORT.encoded_fixture().encode('utf-8')


def test_export_is_repeatable_and_never_patches_global_uuid():
    original_uuid4, original_resource_uuid = uuid.uuid4, EXPORT.staging_resources.uuid
    first = EXPORT.fixture_document()
    second = EXPORT.fixture_document()
    assert first == second
    assert uuid.uuid4 is original_uuid4
    assert EXPORT.staging_resources.uuid is original_resource_uuid


def test_fixture_exercises_real_prefixed_plan_digest_and_rich_resource_contract():
    wire = EXPORT.fixture_document()
    assert wire['status'] == {'configured': True, 'running': False,
        'reachable': False, 'profiles': {}, 'previews_configured': False,
        'can_reconcile': True, 'held_jobs': [{'id': EXPORT.JOB_ID,
            'project': EXPORT.PROJECT, 'recipe': 'static', 'state': 'held',
            'phase': 'held', 'source_commit': 'a' * 40}]}
    assert wire['resources']['next_after'] is None
    resources = wire['resources']['items']
    assert len(resources) == 2
    build = next(r for r in resources if r['kind'] == 'build')
    preview = next(r for r in resources if r['kind'] == 'preview')
    assert build['state'] == preview['state'] == 'ready'
    assert build['created_by'] == preview['created_by'] == 'codex'
    assert build['job_id'] == preview['job_id'] == EXPORT.JOB_ID
    assert preview['keep']['mode'] == 'pinned' and preview['keep']['by'] == 'human:1'
    assert preview['protected'] == [{'reason': 'decision_evidence', 'ref': 'synthetic-wire-review'}]
    assert preview['size_bytes'] is None
    plan = wire['cleanup_plan']
    assert plan['digest'].startswith('sha256:') and len(plan['digest']) == 71
    assert plan['remove'] == [{'id': build['id'], 'kind': 'build', 'size_bytes': 2048, 'why': 'expired'}]
    assert plan['kept'] == [{'id': preview['id'], 'why': 'protected'}]
    assert plan['total_bytes'] == 2048
    assert set(wire) == {'status', 'resources', 'cleanup_plan', 'reconcile'}


def test_actual_http_reconcile_fixture_uses_kind_to_resource_id_map():
    wire = EXPORT.fixture_document()
    result = wire['reconcile']
    assert type(result['resource_ids']) is dict
    assert result['resource_ids'] == {item['kind']: item['id'] for item in wire['resources']['items']}
    assert result['job'] == {'id': EXPORT.JOB_ID, 'state': 'held',
                            'project': EXPORT.PROJECT, 'actor': 'codex', 'environment': 'staging'}
    assert result['phase'] == 'held'
    assert str(uuid.UUID(result['source_id'])) == result['source_id']


def test_check_cli_detects_drift_without_rewriting(monkeypatch, tmp_path):
    fixture = tmp_path / 'staging-wire-v1.json'
    fixture.write_text('{"stale":true}\n')
    monkeypatch.setattr(EXPORT, 'FIXTURE', fixture)
    monkeypatch.setattr('sys.argv', ['export_staging_wire.py', '--check'])
    assert EXPORT.main() == 1
    assert json.loads(fixture.read_text()) == {'stale': True}


def test_write_and_check_cli_use_the_same_producer(monkeypatch, tmp_path):
    fixture = tmp_path / 'fixtures/staging-wire-v1.json'
    monkeypatch.setattr(EXPORT, 'FIXTURE', fixture)
    monkeypatch.setattr('sys.argv', ['export_staging_wire.py', '--write'])
    assert EXPORT.main() == 0
    assert fixture.read_bytes() == EXPORT.encoded_fixture().encode()
    monkeypatch.setattr('sys.argv', ['export_staging_wire.py', '--check'])
    assert EXPORT.main() == 0
