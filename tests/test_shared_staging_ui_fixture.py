"""The dashboard's shared staging fixture must cover exactly the states the Hub can send."""
import json
from pathlib import Path
from typing import get_args

from engineering_hub.shared_staging_api import SharedLifecycleStatus

FIXTURE = Path(__file__).resolve().parents[1] / 'hub-ui' / 'src' / 'fixtures' / 'shared-staging-status-v1.json'


def test_ui_fixture_is_every_valid_shared_status():
    rows = json.loads(FIXTURE.read_text(encoding='utf-8'))
    for row in rows:
        assert SharedLifecycleStatus.model_validate(row).model_dump() == row
    states = get_args(SharedLifecycleStatus.model_fields['state'].annotation)
    assert sorted(row['state'] for row in rows) == sorted(states)
