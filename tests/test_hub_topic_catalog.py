"""Explicit human scope only: fixtures do not provision a real authority."""
import httpx
import pytest

from engineering_hub.registry_bridge import CanonicalRegistryBridge, HumanRegistryAccess
from engineering_hub.registry_client import RegistryAccessDenied, RegistryClient, RegistrySourceUnavailable
from tests.test_hub_api import BASE, hub, human_headers, agent_headers


def bridge(scopes=None, *, resolver=None, reader=None):
    scopes = scopes or {'human:42': frozenset({'engineering-hub'}), 'human:43': frozenset({'client-notes'})}
    def no_remote(_):
        raise AssertionError('Catalog must not fetch snapshots, messages or remote state')
    return CanonicalRegistryBridge(
        RegistryClient('https://registry.test', transport=httpx.MockTransport(no_remote)),
        human_topics=resolver or (lambda principal: scopes.get(principal, frozenset())),
        human_reader=reader or (lambda principal, topic: HumanRegistryAccess(
            'fixture-read-only-never-returned', scopes.get(principal, frozenset()))))


def test_principals_have_disjoint_scope_without_cache_or_readiness():
    current = bridge()
    assert current.topics_for_human('human:42') == ('engineering-hub',)
    assert current.topics_for_human('human:43') == ('client-notes',)
    assert current.topics_for_human('human:42') == ('engineering-hub',)
    assert current.topics_for_human('human:44') == ()
    for actor in ('codex', 'claude', 'hermes', '', 'human:fake', None):
        with pytest.raises(RegistryAccessDenied):
            current.topics_for_human(actor)


@pytest.mark.parametrize('value', [None, [], ['engineering-hub', 'engineering-hub'],
    {'engineering-hub'}, frozenset({'bad:label'}), frozenset({'Uppercase'}),
    frozenset({'../path'}), frozenset({1}), frozenset({'x' * 129}),
    frozenset(f'topic-{index}' for index in range(501))])
def test_invalid_scope_is_not_a_global_topic_catalog(value):
    with pytest.raises(RegistrySourceUnavailable):
        bridge(resolver=lambda _: value).topics_for_human('human:42')


def test_listing_checks_per_topic_membership_too():
    current = bridge(resolver=lambda _: frozenset({'client-notes'}))
    with pytest.raises(RegistryAccessDenied):
        current.topics_for_human('human:42')


def test_missing_authority_is_unavailable_not_empty():
    current = bridge()
    for attribute in ('human_reader', 'human_topics'):
        missing = bridge()
        setattr(missing, attribute, None)
        with pytest.raises(RegistrySourceUnavailable):
            missing.topics_for_human('human:42')
    assert current.topics_for_human('human:42') == ('engineering-hub',)


@pytest.mark.parametrize('failure_at', ['catalog', 'reader'])
def test_authority_errors_are_redacted(failure_at):
    def failure(*_):
        raise RuntimeError('PRIVATE-READER-CREDENTIAL')
    current = bridge(**({'resolver': failure} if failure_at == 'catalog' else {'reader': failure}))
    with pytest.raises(RegistrySourceUnavailable) as result:
        current.topics_for_human('human:42')
    assert 'PRIVATE-READER-CREDENTIAL' not in str(result.value)


def test_default_app_route_is_human_only_and_explicitly_unavailable(hub):
    client, app, settings = hub
    assert client.get(BASE + '/topics').status_code == 401
    assert client.get(BASE + '/topics', headers=agent_headers()).status_code == 403
    human_headers(client, app, settings)
    response = client.get(BASE + '/topics')
    assert response.status_code == 503
    assert response.json() == {'detail': {'code': 'topic_catalog_unavailable'}}


def test_authenticated_route_only_projects_scoped_identifiers(hub, tmp_path):
    from fastapi.testclient import TestClient
    from engineering_hub.app import Settings, create_app
    _, _, settings = hub
    scoped_app = create_app(Settings(tmp_path / 'configured.sqlite3',
        settings.agent_auth, settings.auth_config), registry_bridge=bridge())
    session = scoped_app.state.sessions.create_session(42)
    with TestClient(scoped_app, base_url=settings.auth_config.origin) as browser:
        browser.cookies.set(settings.auth_config.cookie_name, session['cookie_token'])
        response = browser.get(BASE + '/topics')
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert response.json() == {'items': [{'topic': 'engineering-hub',
            'last_activity_at': None, 'peer_count': None}]}
        assert 'fixture-read-only' not in response.text
        assert 'client-notes' not in response.text
        assert browser.post(BASE + '/topics').status_code == 405
