"""Durable first-verified identity pins; no live Access account enrollment."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi import HTTPException

from engineering_hub.auth import AuthConfig, SessionStore
from engineering_hub.cloudflare_access import CloudflareIdentity


CONFIG = AuthConfig(
    "https://hub.example", human_auth_mode="cloudflare_access",
    cloudflare_issuer="https://fixture-team.cloudflareaccess.com",
    cloudflare_audience="fixture-eh-only-audience",
    cloudflare_allowed_emails=("operator@first.example", "operator@second.example"),
)


def identity(subject, *, email=CONFIG.cloudflare_allowed_emails[0], expires=10000):
    actor = "human:cf:" + hashlib.sha256("\0".join((
        CONFIG.cloudflare_issuer, CONFIG.cloudflare_audience, subject,
    )).encode()).hexdigest()
    return CloudflareIdentity(subject, email, actor, expires)


def pin_rows(store):
    with store._read_db() as db:
        return db.execute("SELECT identity_key, binding, actor FROM auth_access_identities ORDER BY identity_key").fetchall()


@pytest.mark.parametrize("same_subject", [True, False])
def test_concurrent_first_logins_serialize_identity_enrollment(tmp_path, same_subject):
    path = tmp_path / "hub.sqlite"
    stores = [SessionStore(path, clock=lambda: 1000) for _ in range(2)]
    identities = [identity("first"), identity("first" if same_subject else "second")]
    barrier = Barrier(2)

    def login(index):
        barrier.wait(timeout=5)
        try:
            return (index, stores[index].create_cloudflare_session(CONFIG, identities[index]))
        except HTTPException as error:
            assert error.status_code == 403
            return (index, None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(login, range(2)))
    accepted = [(index, session) for index, session in outcomes if session is not None]
    assert len(accepted) == (2 if same_subject else 1)
    rows = pin_rows(stores[0])
    assert len(rows) == 1
    for index, session in accepted:
        assert rows[0][2] == identities[index].actor
        assert stores[index].cloudflare_session_for(session["cookie_token"], CONFIG, identities[index])
    with stores[0]._read_db() as db:
        assert db.execute("SELECT count(*) FROM auth_access_sessions").fetchone()[0] == len(accepted)


@pytest.mark.parametrize("end_session", ["logout", "expire"])
def test_pin_persists_after_session_end_and_store_reopen(tmp_path, end_session):
    path = tmp_path / "hub.sqlite"
    now = [1000]
    store = SessionStore(path, session_ttl=10, clock=lambda: now[0])
    original = identity("first")
    session = store.create_cloudflare_session(CONFIG, original)
    before = pin_rows(store)
    if end_session == "logout":
        store.delete_cloudflare_session(session["cookie_token"])
    else:
        now[0] += 11
    reopened = SessionStore(path, clock=lambda: now[0])
    assert reopened.cloudflare_session_for(session["cookie_token"], CONFIG, original) is None
    with pytest.raises(HTTPException) as error:
        reopened.create_cloudflare_session(CONFIG, identity("replacement"))
    assert error.value.status_code == 403
    assert pin_rows(reopened) == before
    renewed = reopened.create_cloudflare_session(CONFIG, original)
    assert reopened.cloudflare_session_for(renewed["cookie_token"], CONFIG, original)["actor"] == original.actor
    assert pin_rows(reopened) == before


def test_failed_identity_repin_does_not_rotate_or_delete_old_session(tmp_path):
    store = SessionStore(tmp_path / "hub.sqlite", clock=lambda: 1000)
    original = identity("first")
    session = store.create_cloudflare_session(CONFIG, original)
    before = pin_rows(store)
    with pytest.raises(HTTPException) as error:
        store.create_cloudflare_session(CONFIG, identity("replacement"), previous_token=session["cookie_token"])
    assert error.value.status_code == 403
    assert pin_rows(store) == before
    assert store.cloudflare_session_for(session["cookie_token"], CONFIG, original)["actor"] == original.actor
    with store._read_db() as db:
        assert db.execute("SELECT count(*) FROM auth_access_sessions").fetchone()[0] == 1


def test_successful_rotation_replaces_only_requested_session_and_preserves_pin(tmp_path):
    store = SessionStore(tmp_path / "hub.sqlite", clock=lambda: 1000)
    operator = identity("first")
    previous = store.create_cloudflare_session(CONFIG, operator)
    independent = store.create_cloudflare_session(CONFIG, operator)
    before = pin_rows(store)
    replacement = store.create_cloudflare_session(CONFIG, operator, previous_token=previous["cookie_token"])
    assert store.cloudflare_session_for(previous["cookie_token"], CONFIG, operator) is None
    assert store.cloudflare_session_for(independent["cookie_token"], CONFIG, operator)
    assert store.cloudflare_session_for(replacement["cookie_token"], CONFIG, operator)
    assert pin_rows(store) == before


def test_two_allowlisted_emails_have_independent_durable_pins(tmp_path):
    store = SessionStore(tmp_path / "hub.sqlite", clock=lambda: 1000)
    first = identity("first")
    second = identity("second", email=CONFIG.cloudflare_allowed_emails[1])
    for operator in (first, second):
        session = store.create_cloudflare_session(CONFIG, operator)
        assert store.cloudflare_session_for(session["cookie_token"], CONFIG, operator)["actor"] == operator.actor
    assert len(pin_rows(store)) == 2
