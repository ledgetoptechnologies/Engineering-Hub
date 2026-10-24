"""Synthetic interactive-review wire examples from the fully composed Hub app.

Uses only a temporary ledger/config, synthetic in-process authentication and
ASGI requests. No asset client, worker, external network or live credentials.
UUID/time patches are confined to the two catalog/review module namespaces.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from fastapi.testclient import TestClient
from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub import components, interactive_reviews


FIXTURE = ROOT / 'hub-ui/src/fixtures/interactive-reviews-wire-v1.json'
NAMESPACE = uuid.UUID('e4095fa1-7d91-4105-a80c-34ee3714e4ee')
NOW = 1800000000
BASE = '/api/hub/v1'


class FixedTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(NOW, tz or timezone.utc)


def fixture_document():
    for module in (components, interactive_reviews):
        if Path(module.__file__).resolve().parent != ROOT / 'src/engineering_hub':
            raise RuntimeError('Use this worktree source')
    original_uuid4, original_datetime = uuid.uuid4, datetime
    counter = iter(range(1, 100))
    synthetic_uuid = SimpleNamespace(UUID=uuid.UUID,
        uuid4=lambda: uuid.uuid5(NAMESPACE, 'wire-' + str(next(counter))))
    with ExitStack() as stack:
        temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='eh-interactive-wire-')))
        for module in (components, interactive_reviews):
            stack.enter_context(patch.object(module, 'uuid', synthetic_uuid))
            stack.enter_context(patch.object(module, 'datetime', FixedTime))
        config = temporary / 'interactive-service.json'
        config.write_text(json.dumps({'schema': 1, 'enabled': True, 'assets': {}, 'grants': {
            'ledge-top-ops': {'codex': ['review_read', 'review_create'],
                             'human:42': ['review_read', 'review_answer']}}}))
        config.chmod(0o600)
        settings = Settings(temporary / 'hub.sqlite3',
            AuthSettings('synthetic-wire-hermes', 'synthetic-wire-codex', claude_token='synthetic-wire-claude'),
            AuthConfig('https://hub.fixture.test', beau_github_id=42))
        app = create_app(settings)
        with TestClient(app, base_url=settings.auth_config.origin) as client:
            session = app.state.sessions.create_session(42)
            client.cookies.set(settings.auth_config.cookie_name, session['cookie_token'])
            human = {'Origin': settings.auth_config.origin, 'X-Hub-CSRF': session['csrf_token']}
            component = client.post(BASE + '/projects/ledge-top-ops/components', headers=human,
                json={'slug': 'synthetic-reviewer', 'name': 'Synthetic reviewer',
                      'client_request_id': 'wire-component', 'kind': 'web_app', 'status': 'active'})
            component.raise_for_status()
            component = component.json()
            created = client.post(BASE + '/interactive-reviews',
                headers={'Authorization': 'Bearer synthetic-wire-codex'}, json={
                    'project': 'ledge-top-ops', 'component_id': component['id'], 'component_revision': component['revision'],
                    'topic': 'synthetic-phone-review', 'dataset_digest': 'sha256:' + 'a' * 64,
                    'schema': 'choice-text.v1', 'title': 'Synthetic item review', 'client_request_id': 'wire-review',
                    'items': [{'id': 'classification', 'title': 'Classification', 'prompt': 'Choose a synthetic label.',
                               'choices': [{'id': 'keep', 'label': 'Keep'}, {'id': 'skip', 'label': 'Skip'}]},
                              {'id': 'note', 'title': 'Notes', 'prompt': 'Add a short synthetic note.'}]})
            created.raise_for_status()
            identity = created.json()['id']
            path = BASE + '/interactive-reviews/' + identity
            responses = {
                'list': client.get(BASE + '/interactive-reviews', params={'project': 'ledge-top-ops'}),
                'detail': client.get(path),
                'state': client.get(path + '/state'),
                'answered': client.post(path + '/answers', headers=human, json={'expected_revision': 1,
                    'client_request_id': 'wire-choice', 'answers': [{'item_id': 'classification', 'value': 'keep'}]}),
            }
            responses['conflict'] = client.post(path + '/answers', headers=human, json={'expected_revision': 1,
                'client_request_id': 'wire-stale', 'answers': [{'item_id': 'classification', 'value': 'skip'}]})
            responses['completed'] = client.post(path + '/answers', headers=human, json={'expected_revision': 2,
                'client_request_id': 'wire-complete', 'answers': [{'item_id': 'note', 'value': 'Synthetic human note'}], 'complete': True})
            responses['forbidden'] = client.get(path, headers={'Authorization': 'Bearer synthetic-wire-claude'})
            for key in ('list', 'detail', 'state', 'answered', 'completed'):
                responses[key].raise_for_status()
            if responses['conflict'].status_code != 409 or responses['forbidden'].status_code != 403:
                raise RuntimeError('Expected real conflict and forbidden responses')
        if uuid.uuid4 is not original_uuid4 or datetime is not original_datetime:
            raise RuntimeError('Global UUID/time namespace changed')
        return {key: {'status': response.status_code, 'body': response.json()}
                for key, response in responses.items()}


def encoded_fixture():
    return json.dumps(fixture_document(), sort_keys=True, indent=2, ensure_ascii=True) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--write', action='store_true')
    mode.add_argument('--check', action='store_true')
    args = parser.parse_args()
    expected = encoded_fixture().encode()
    if args.write:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_bytes(expected)
        print('Wrote interactive-reviews-wire-v1.json')
        return 0
    if not FIXTURE.exists() or FIXTURE.read_bytes() != expected:
        print('Interactive-review wire fixture differs; run --write', file=sys.stderr)
        return 1
    print('Interactive-review wire fixture matches actual Hub HTTP')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
