"""Authenticated archive HTTP projection; no live archive or production writes.

Linux archive fixtures use actual SQLite/owned FDs with explicitly mocked RO
mount flags. Pure DTO/disabled-auth cases also run on Windows. These tests do
not establish a real readonly mount; that belongs to separate kernel acceptance.
"""
from contextlib import closing
import hashlib
import json
import sqlite3
import sys

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from agent_message_bus.auth import AuthSettings
from agent_message_bus.database import MessageRepository
from engineering_hub import archive_messages as reader_module
from engineering_hub.archive_api import attach_archive_routes, ArchiveMessageDTO, ArchivePageDTO
from engineering_hub.archive_messages import ArchiveMessageReader
from engineering_hub.auth import AuthConfig, SessionStore, require_human
from engineering_hub.repository import HubRepository


BASE = '/api/hub/v1/archive/messages'
IDS = [f'00000000-0000-0000-0000-{i:012d}' for i in range(1, 6)]


def projected():
    return dict(archive_inert=True, id=IDS[0], sender='hermes', recipient='codex', message_type='discussion',
                priority='normal', body={'text': 'original body'}, status='pending', correlation_id='topic-alpha',
                in_reply_to=None, created_at='2026-10-01T00:00:00Z')


def build(tmp_path, reader):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    auth = AuthSettings('test-hermes', 'test-codex', claude_token='test-claude')
    config = AuthConfig('https://hub.test', beau_github_id=42)
    sessions = SessionStore(repo.path, repository=repo)
    app = FastAPI()
    def actor(request):
        # Same identity selection as the real Hub: supplied bearer is always
        # validated, never falls back to a valid human session when invalid.
        if request.headers.get('authorization'):
            return auth.agent(request)
        return require_human(request, config, sessions)
    attach_archive_routes(app, reader=reader, actor=actor)
    return TestClient(app, base_url=config.origin), config, sessions, repo


@pytest.fixture
def disabled(tmp_path):
    client, config, sessions, repo = build(tmp_path, None)
    with client:
        yield client, config, sessions, repo


@pytest.fixture
def archive(tmp_path, monkeypatch):
    if sys.platform != 'linux':
        pytest.skip('Actual Linux readonly immutable archive FD required')
    path = tmp_path / 'approved-legacy.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        db.execute(MessageRepository._migration_table_sql().replace('messages__migration', 'messages'))
        for i, (sender, recipient, status) in enumerate([
                ('hermes', 'codex', 'pending'), ('codex', 'claude', 'acknowledged'),
                ('hermes', 'claude', 'claimed'), ('claude', 'codex', 'rejected')]):
            values = (IDS[i], sender, recipient, 'discussion', 'normal', json.dumps({'original': i}), status,
                      'topic-alpha', IDS[0] if i else None, 0, 0, '2026-10-01T00:00:00Z',
                      None, None, None, None, None)
            db.execute('INSERT INTO messages VALUES (' + ','.join('?' for _ in values) + ')', values)
        db.commit()
    before = path.read_bytes()
    monkeypatch.setattr(reader_module, '_readonly', lambda fd: True)  # TEST ONLY, not kernel RO proof.
    reader = ArchiveMessageReader(path, expected_sha256=hashlib.sha256(before).hexdigest(), maximum_database_bytes=1024**2)
    client, config, sessions, repo = build(tmp_path, reader)
    try:
        with client:
            yield client, config, sessions, repo, reader, path, before
    finally:
        reader.close()


def headers(agent='codex'):
    return {'Authorization': 'Bearer test-' + agent}


@pytest.mark.parametrize('agent,expected', [('codex', [IDS[0], IDS[1], IDS[3]]),
                                         ('hermes', [IDS[0], IDS[2]]), ('claude', [IDS[1], IDS[2], IDS[3]])])
def test_actual_reader_visibility_is_sender_or_recipient_only(archive, agent, expected):
    client = archive[0]
    response = client.get(BASE, headers=headers(agent))
    assert response.status_code == 200
    page = response.json()
    assert page['archive_inert'] is True and [item['id'] for item in page['items']] == expected
    assert all(item['archive_inert'] is True for item in page['items'])
    assert all(set(item) == set(projected()) for item in page['items'])
    assert page['next_after_id'] is None


def test_authenticated_human_session_sees_all_and_bad_bearer_never_falls_back(archive):
    client, config, sessions, *_ = archive
    session = sessions.create_session(42)
    client.cookies.set(config.cookie_name, session['cookie_token'])
    assert len(client.get(BASE).json()['items']) == 4
    for value in ('Bearer invalid', '', 'Basic wrong', 'Bearer  test-codex'):
        assert client.get(BASE, headers={'Authorization': value}).status_code == 401
    assert client.get(BASE, headers=[('Authorization', 'Bearer test-codex'), ('Authorization', 'Bearer test-hermes')]).status_code == 401
    assert len(client.get(BASE, headers=headers('codex')).json()['items']) == 3


def test_denied_and_missing_message_are_same404_and_pagination_is_inert(archive):
    client = archive[0]
    responses = [client.get(BASE + '/' + identity, headers=headers()) for identity in (IDS[2], IDS[4])]
    assert all(response.status_code == 404 for response in responses)
    assert responses[0].json() == responses[1].json() == {'detail': {'code': 'archive_message_not_found'}}
    first = client.get(BASE + '?limit=2', headers=headers()).json()
    second = client.get(BASE + '?limit=2&after_id=' + first['next_after_id'], headers=headers()).json()
    assert [item['id'] for item in first['items']] == [IDS[0], IDS[1]]
    assert [item['id'] for item in second['items']] == [IDS[3]]
    assert second['next_after_id'] is None
    item = client.get(BASE + '/' + IDS[0], headers=headers()).json()
    assert item['status'] == 'pending' and item['body'] == {'original': 0} and item['archive_inert'] is True


@pytest.mark.parametrize('query', ['actor=human:42', 'owner=human:42', 'recipient=codex', 'sender=hermes',
                                  'sql=SELECT', 'limit=0', 'limit=51', 'limit=true', 'limit=01',
                                  'limit=1&limit=2', 'after_id=bad', 'after_id=' + IDS[0] + '&after_id=' + IDS[1]])
def test_closed_query_rejects_selectors_and_invalid_input(archive, query):
    response = archive[0].get(BASE + '?' + query, headers=headers())
    assert response.status_code == 422 and response.json() == {'detail': {'code': 'invalid_archive_input'}}


def test_single_get_accepts_no_query_or_body_identity_selector(archive):
    client = archive[0]
    assert client.get(BASE + '/' + IDS[0] + '?actor=human:42', headers=headers()).status_code == 422
    assert client.get(BASE + "/'OR1=1", headers=headers()).status_code == 422
    assert client.request('GET', BASE, headers=headers(), json={'actor': 'human:42'}).status_code == 422
    assert client.request('GET', BASE + '/' + IDS[0], headers=headers(), json={'owner': 'human:42'}).status_code == 422


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE'])
def test_write_verbs_unavailable_and_no_archive_or_hub_mutation(archive, method):
    client, _, _, repo, _, path, before = archive
    counts = (len(repo.list_tasks()), len(repo.list_decisions()), len(repo.list_runs()), repo.get_paused())
    for endpoint in (BASE, BASE + '/' + IDS[0]):
        assert client.request(method, endpoint, headers=headers(), json={'status': 'acknowledged'}).status_code == 405
    assert path.read_bytes() == before
    assert (len(repo.list_tasks()), len(repo.list_decisions()), len(repo.list_runs()), repo.get_paused()) == counts


def test_disabled_archive_authenticates_before_availability_or_query_validation(disabled):
    client, config, sessions, _ = disabled
    for path in (BASE, BASE + '/' + IDS[0], BASE + '?owner=human:42'):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={'Authorization': 'Bearer invalid'}).status_code == 401
    for path in (BASE, BASE + '/' + IDS[0]):
        response = client.get(path, headers=headers())
        assert response.status_code == 503 and response.json() == {'detail': {'code': 'archive_unavailable'}}
    session = sessions.create_session(42)
    client.cookies.set(config.cookie_name, session['cookie_token'])
    assert client.get(BASE).status_code == 503
    assert client.get(BASE, headers={'Authorization': 'Bearer invalid'}).status_code == 401


def test_closed_reader_and_bad_projection_return_only_sanitized503(archive, monkeypatch):
    client, _, _, _, reader, *_ = archive
    original = reader.get_message
    monkeypatch.setattr(reader, 'get_message', lambda actor, identity: {**original(actor, identity), 'private_field': 'private-sentinel'})
    response = client.get(BASE + '/' + IDS[0], headers=headers())
    assert response.status_code == 503 and 'private-sentinel' not in response.text
    assert response.json() == {'detail': {'code': 'archive_unavailable'}}
    monkeypatch.setattr(reader, 'get_message', original)
    reader.close()
    assert client.get(BASE, headers=headers()).status_code == 503


@pytest.mark.parametrize('change', [{'archive_inert': 1}, {'archive_inert': False}, {'private_field': 'private-sentinel'},
                                   {'sender': 'owner'}, {'body': object()}, {'body': float('nan')},
                                   {'created_at': '2026-10-01T00:00:00'}])
def test_response_projection_is_strict_closed_and_inert(change):
    item = projected()
    item.update(change)
    with pytest.raises(ValidationError):
        ArchiveMessageDTO.model_validate(item)


def test_page_schema_forbids_private_fields_and_unbounded_rows():
    for page in (dict(archive_inert=True, items=[projected()] * 51, next_after_id=None),
                 dict(archive_inert=True, items=[], next_after_id=None, private_field='private-sentinel')):
        with pytest.raises(ValidationError):
            ArchivePageDTO.model_validate(page)


def test_configuration_requires_exact_reader_and_callable_server_auth():
    with pytest.raises(ValueError):
        attach_archive_routes(FastAPI(), reader=object(), actor=lambda request: 'human:42')
    with pytest.raises(ValueError):
        attach_archive_routes(FastAPI(), reader=None, actor=None)
