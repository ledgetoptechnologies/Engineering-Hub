"""Agents map data: routing counts for humans, with no message content."""
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from agent_message_bus.auth import AuthSettings
from agent_message_bus.database import MessageRepository
from engineering_hub.agent_activity_api import attach_agent_activity_route, project_activity
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.private_gateway import browser_route_allowed
from engineering_hub.repository import HubError

SECRET = 'body text that must never appear'
PATH = '/api/hub/v1/agents/activity'


@pytest.fixture
def repo(tmp_path):
    repository = MessageRepository(tmp_path / 'bus.sqlite3')
    repository.initialize()
    return repository


def send(repo, sender, recipient, body, message_type='request'):
    return repo.insert(sender=sender, recipient=recipient, message_type=message_type, body=body)


def activity(repo):
    return project_activity(repo.routing_activity(since='2000-01-01T00:00:00Z'), observed_at='now')


def by_topic(result):
    return {item['topic']: item for item in result['topics']}


def test_counts_open_messages_by_recipient_and_topic(repo):
    send(repo, 'codex', 'hermes', {'topic': 'staging', 'text': SECRET})
    send(repo, 'claude', 'hermes', {'topic': 'staging'})
    claimed = send(repo, 'hermes', 'codex', {'topic': 'staging'})
    repo.claim(claimed.id, 'codex')
    result = activity(repo)
    staging = by_topic(result)['staging']
    assert (staging['pending'], staging['claimed'], staging['messages']) == (2, 1, 3)
    assert staging['agents'] == ['claude', 'codex', 'hermes']
    assert staging['pending_by_agent'] == {'hermes': 2}
    assert staging['claimed_by_agent'] == {'codex': 1}
    agents = {item['agent']: item for item in result['agents']}
    assert (agents['hermes']['pending'], agents['codex']['claimed']) == (2, 1)
    assert agents['hermes']['oldest_pending_at'] is not None
    assert agents['claude']['oldest_pending_at'] is None
    assert result['open_total'] == 3


def test_acknowledged_message_keeps_the_link_but_is_not_open(repo):
    done = send(repo, 'codex', 'claude', {'topic': 'viewer'})
    repo.claim(done.id, 'claude')
    repo.acknowledge(done.id, 'claude')
    result = activity(repo)
    viewer = by_topic(result)['viewer']
    assert (viewer['pending'], viewer['claimed'], viewer['messages']) == (0, 0, 1)
    assert result['open'] == [] and result['open_total'] == 0


@pytest.mark.parametrize('body', [{'text': 'no topic'}, {'topic': 7}, {'topic': 'bad\ntopic'}, {'topic': '   '}, {'topic': 'x' * 129}, 'plain', [1]])
def test_unusable_topic_is_reported_as_unrouted(repo, body):
    send(repo, 'codex', 'claude', body)
    result = activity(repo)
    assert by_topic(result)[None]['pending'] == 1
    assert result['open'][0]['topic'] is None


def test_topic_names_may_hold_spaces(repo):
    send(repo, 'codex', 'claude', {'topic': 'LT-ops Viewer readiness'})
    assert by_topic(activity(repo))['LT-ops Viewer readiness']['pending'] == 1


def test_links_outside_the_window_drop_but_open_messages_stay(repo):
    send(repo, 'codex', 'claude', {'topic': 'old'})
    result = project_activity(repo.routing_activity(since='2999-01-01T00:00:00Z'), observed_at='now')
    assert by_topic(result)['old']['messages'] == 0
    assert by_topic(result)['old']['pending'] == 1


def test_result_never_contains_a_body_and_unsafe_types_are_masked(repo):
    send(repo, 'codex', 'claude', {'topic': 'safe', 'text': SECRET}, message_type='has space\n')
    result = activity(repo)
    assert SECRET not in json.dumps(result)
    assert set(result['open'][0]) == {'id', 'sender', 'recipient', 'message_type', 'priority',
                                      'status', 'created_at', 'topic'}
    assert result['open'][0]['message_type'] == 'other'


def test_open_list_is_capped_but_the_total_is_not(repo):
    for _ in range(3):
        send(repo, 'codex', 'claude', {'topic': 'busy'})
    raw = repo.routing_activity(since='2000-01-01T00:00:00Z', open_limit=2)
    result = project_activity(raw, observed_at='now')
    assert len(result['open']) == 2 and result['open_total'] == 3


def test_scans_are_bounded_but_the_total_stays_true(repo):
    for _ in range(3):
        send(repo, 'codex', 'claude', {'topic': 'busy'})
    raw = repo.routing_activity(since='2000-01-01T00:00:00Z', scan_limit=2)
    assert sum(row['total'] for row in raw['open_counts']) == 2
    assert sum(row['total'] for row in raw['links']) == 2
    assert raw['open_total'] == 3


def test_busy_topics_survive_the_topic_cap():
    links = [dict(sender='codex', recipient='claude', topic=f'quiet-{index:03}', total=1, latest='t')
             for index in range(250)]
    counts = [dict(recipient='claude', topic='zz-busy', status='pending', total=1, oldest='t')]
    result = project_activity(dict(open_counts=counts, links=links, open=[], open_total=1), observed_at='now')
    assert len(result['topics']) == 200 and result['topics'][0]['topic'] == 'zz-busy'


def test_route_is_human_only_and_needs_the_bus(tmp_path):
    settings = Settings(tmp_path / 'hub.sqlite3', AuthSettings('test-hermes', 'test-codex', claude_token='test-claude'),
                        AuthConfig('https://hub.test', beau_github_id=42))
    app = create_app(settings)
    with TestClient(app, base_url=settings.auth_config.origin) as client:
        assert client.get(PATH).status_code == 401
        refused = client.get(PATH, headers={'Authorization': 'Bearer test-codex'})
        assert refused.status_code == 403 and refused.json()['detail']['code'] == 'human_session_required'
        session = app.state.sessions.create_session(42)
        client.cookies.set(settings.auth_config.cookie_name, session['cookie_token'])
        assert client.get(PATH).status_code == 503


def activity_client(repo, owner='human:42', actor='human:42'):
    app = FastAPI()

    @app.exception_handler(HubError)
    async def refused(request, error):
        return JSONResponse({'detail': {'code': error.code}}, status_code=error.status)

    attach_agent_activity_route(app, bus_service=SimpleNamespace(repository=repo),
                                sessions=owner and SimpleNamespace(owner=owner), human=lambda request: actor)
    return TestClient(app)


def test_owner_gets_counts_and_no_body(repo):
    send(repo, 'codex', 'claude', {'topic': 'live topic', 'text': SECRET})
    response = activity_client(repo).get(PATH)
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert SECRET not in response.text
    assert response.json()['topics'][0]['pending_by_agent'] == {'claude': 1}


def test_another_human_is_refused(repo):
    refused = activity_client(repo, actor='human:7').get(PATH)
    assert refused.status_code == 403 and refused.json()['detail']['code'] == 'agent_activity_owner_required'


def test_no_owner_record_means_unavailable_not_open(repo):
    assert activity_client(repo, owner=None).get(PATH).status_code == 503


def test_gateway_exposes_only_the_read():
    assert browser_route_allowed('GET', PATH)
    assert not browser_route_allowed('POST', PATH)
    assert not browser_route_allowed('GET', PATH + '/x')
