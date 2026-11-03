"""Draft storage tests, NOT ingress or OAuth integration tests."""
import pytest
from engineering_hub.auth import SessionStore
from engineering_hub.bound_oauth_draft import BoundOAuthDraftStore


def test_state_binding_and_one_use(tmp_path):
    store = BoundOAuthDraftStore(SessionStore(tmp_path/'hub.db'))
    state, _ = store.create_state('https://issuer', 'aud', 'subject', 123)
    assert store.consume_state(state, 'https://issuer', 'aud', 'other', 123) is None
    assert store.consume_state(state, 'https://issuer', 'other', 'subject', 123) is None
    assert store.consume_state(state, 'https://issuer', 'aud', 'subject', 123)
    assert store.consume_state(state, 'https://issuer', 'aud', 'subject', 123) is None


def test_session_binding_revoke_and_expiry(tmp_path):
    now = [1000]
    backing = SessionStore(tmp_path/'hub.db', session_ttl=10, clock=lambda:now[0])
    store = BoundOAuthDraftStore(backing)
    with pytest.raises(ValueError):
        store.create_session('issuer', 'aud', 'subject', 123, 124)
    token, csrf = store.create_session('issuer', 'aud', 'subject', 123, 123)
    assert store.session_for(token, 'issuer', 'aud', 'subject', 123)['csrf_token'] == csrf
    for issuer, audience, subject, github in [('other','aud','subject',123),('issuer','other','subject',123),('issuer','aud','other',123),('issuer','aud','subject',124)]:
        assert store.session_for(token, issuer, audience, subject, github) is None
    now[0] += 10
    assert store.session_for(token, 'issuer', 'aud', 'subject', 123) is None
    store.delete_session(token)
