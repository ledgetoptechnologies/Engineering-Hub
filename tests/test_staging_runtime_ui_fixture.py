"""The dashboard's runtime fixture must stay a snapshot the Hub would actually send."""
from datetime import datetime, timedelta
import json
from pathlib import Path

from engineering_hub.staging_runtime import validate_runtime_snapshot

FIXTURE = Path(__file__).resolve().parents[1] / 'hub-ui' / 'src' / 'fixtures' / 'staging-runtime-wire-v1.json'


def test_ui_wire_fixture_is_a_valid_hub_runtime_response():
    body = json.loads(FIXTURE.read_text(encoding='utf-8'))
    assert set(body) == {'configured', 'state', 'snapshot'}
    snapshot = body['snapshot']
    observed = datetime.fromisoformat(snapshot['observed_at'].replace('Z', '+00:00'))
    assert validate_runtime_snapshot(snapshot, now=observed + timedelta(seconds=30)) == snapshot
