"""Export deterministic synthetic component responses through the real Hub API.

Temporary SQLite and synthetic sessions only: no service, host, credential or
execution access. Module-local time/UUID providers are restored after export.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'src'
sys.path.insert(0, str(SOURCE))

from fastapi.testclient import TestClient

from agent_message_bus.auth import AuthSettings
from engineering_hub import components
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig


FIXTURE = ROOT / 'hub-ui/src/fixtures/components-wire-v1.json'
PROJECT = 'treedar'
NAMESPACE = uuid.UUID('de1197ca-86d1-42c3-b6aa-d79736e0cf15')
COMPONENT_ID = str(uuid.uuid5(NAMESPACE, 'synthetic-component'))
NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)


def fixture_document():
    if Path(components.__file__).resolve().parent != SOURCE / 'engineering_hub':
        raise RuntimeError('Use this worktree source for the wire fixture')
    local_uuid = SimpleNamespace(UUID=uuid.UUID, uuid4=lambda: uuid.UUID(COMPONENT_ID))
    local_datetime = SimpleNamespace(now=lambda zone: NOW.astimezone(zone))
    with ExitStack() as stack:
        scratch = stack.enter_context(tempfile.TemporaryDirectory(prefix='eh-components-wire-'))
        stack.enter_context(patch.object(components, 'uuid', local_uuid))
        stack.enter_context(patch.object(components, 'datetime', local_datetime))
        settings = Settings(Path(scratch) / 'hub.sqlite3',
            AuthSettings('synthetic-hermes', 'synthetic-codex', claude_token='synthetic-claude'),
            AuthConfig('https://hub.invalid', beau_github_id=1))
        app = create_app(settings)
        client = stack.enter_context(TestClient(app, base_url=settings.auth_config.origin))
        session = app.state.sessions.create_session(1)
        client.cookies.set(settings.auth_config.cookie_name, session['cookie_token'])
        human = {'Origin': settings.auth_config.origin, 'X-Hub-CSRF': session['csrf_token']}
        path = '/api/hub/v1/projects/' + PROJECT + '/components'
        payload = {'slug': 'pointcloud-review', 'name': 'Pointcloud review',
            'kind': 'web_app', 'status': 'planned',
            'description': 'Synthetic component catalog example; metadata only.',
            'repository_url': 'https://github.com/example/synthetic-component',
            'client_request_id': 'synthetic-component-wire-v1'}
        created = client.post(path, headers=human, json=payload)
        created.raise_for_status()
        single_path = '/api/hub/v1/components/' + COMPONENT_ID
        responses = {
            'list': (200, client.get(path)),
            'single': (200, client.get(single_path)),
            'conflict': (409, client.patch(single_path, headers=human,
                json={'expected_revision': 2, 'name': 'Stale revision'})),
            # Humans still need CSRF; authenticated agent catalog writes are allowed.
            'forbidden': (403, client.post(path, json=payload)),
            'validation': (422, client.post(path, headers=human,
                json={**payload, 'kind': 'unregistered-runtime'})),
        }
        document = {}
        for name, (expected, response) in responses.items():
            if response.status_code != expected:
                raise RuntimeError('Synthetic component response changed: ' + name)
            document[name] = {'status': response.status_code, 'body': response.json()}
        return document


def encoded_fixture():
    return json.dumps(fixture_document(), sort_keys=True, indent=2, ensure_ascii=True) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--write', action='store_true')
    mode.add_argument('--check', action='store_true')
    args = parser.parse_args()
    expected = encoded_fixture()
    if args.write:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(expected, encoding='utf-8', newline='\n')
        print('Wrote hub-ui/src/fixtures/components-wire-v1.json')
        return 0
    if not FIXTURE.is_file() or FIXTURE.read_bytes() != expected.encode('utf-8'):
        print('Components wire fixture differs; run scripts/export_components_wire.py --write', file=sys.stderr)
        return 1
    print('Components wire fixture matches actual API responses')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
