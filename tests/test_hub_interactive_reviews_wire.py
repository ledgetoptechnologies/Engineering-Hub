import importlib.util
from pathlib import Path
import uuid


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('export_interactive_reviews_wire',
    ROOT / 'scripts/export_interactive_reviews_wire.py')
EXPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPORT)


def test_tracked_interactive_fixture_matches_fully_composed_http():
    assert EXPORT.FIXTURE.read_bytes() == EXPORT.encoded_fixture().encode()


def test_repeatable_local_namespace_patches_and_real_error_contracts():
    original_uuid = uuid.uuid4
    original_component_uuid = EXPORT.components.uuid
    original_review_time = EXPORT.interactive_reviews.datetime
    wire = EXPORT.fixture_document()
    assert wire == EXPORT.fixture_document()
    assert uuid.uuid4 is original_uuid
    assert EXPORT.components.uuid is original_component_uuid
    assert EXPORT.interactive_reviews.datetime is original_review_time
    assert set(wire) == {'list', 'detail', 'state', 'answered', 'completed', 'conflict', 'forbidden'}
    assert all(set(response) == {'status', 'body'} for response in wire.values())
    assert wire['conflict']['status'] == 409
    assert wire['forbidden']['status'] == 403
    assert wire['answered']['body']['answers']['classification']['by'] == 'human:42'
    assert wire['completed']['body']['state'] == 'completed'
    assert wire['completed']['body']['revision'] == 3
    assert wire['detail']['body']['component_revision'] == 1
    assert wire['detail']['body']['dataset_digest'] == 'sha256:' + 'a' * 64


def test_wire_check_and_write_modes_are_identical(monkeypatch, tmp_path):
    target = tmp_path / 'interactive-reviews-wire-v1.json'
    monkeypatch.setattr(EXPORT, 'FIXTURE', target)
    monkeypatch.setattr('sys.argv', ['export_interactive_reviews_wire.py', '--check'])
    assert EXPORT.main() == 1
    monkeypatch.setattr('sys.argv', ['export_interactive_reviews_wire.py', '--write'])
    assert EXPORT.main() == 0
    monkeypatch.setattr('sys.argv', ['export_interactive_reviews_wire.py', '--check'])
    assert EXPORT.main() == 0
