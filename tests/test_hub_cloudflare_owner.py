"""Synthetic Access identities retain local ownership and reader boundaries."""
import hashlib
import sqlite3
import sys

import pytest

from engineering_hub.auth import AuthConfig, SessionStore
from engineering_hub.cloudflare_access import CloudflareIdentity
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.registry_bridge import CanonicalRegistryBridge, HumanRegistryAccess
from engineering_hub.registry_client import RegistryAccessDenied, RegistrySourceUnavailable
from engineering_hub.repository import HubError, HubRepository


CONFIG = AuthConfig(
    "https://hub.example", human_auth_mode="cloudflare_access",
    cloudflare_issuer="https://fixture-team.cloudflareaccess.com",
    cloudflare_audience="fixture-eh-only-audience",
    cloudflare_allowed_emails=("operator@first.example", "operator@second.example"),
)
SUBJECT = "synthetic-operator"
ACTOR = "human:cf:" + hashlib.sha256("\0".join((
    CONFIG.cloudflare_issuer, CONFIG.cloudflare_audience, SUBJECT,
)).encode()).hexdigest()
IDENTITY = CloudflareIdentity(SUBJECT, CONFIG.cloudflare_allowed_emails[0], ACTOR, 2000)


def schema(path):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        return db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()


def denied(action):
    with pytest.raises(HubError, match="^pilot_ownership_required$") as error:
        action()
    assert error.value.status == 409


@pytest.fixture
def enrolled(tmp_path):
    if sys.platform != "linux":
        pytest.skip("Linux kernel pilot ownership capability")
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "hub.sqlite"
    with PilotOwner(path, allow_ephemeral_test_storage=True) as owner:
        repo = HubRepository(path, pilot_owner=owner)
        yield path, owner, SessionStore(path, repository=repo, clock=lambda: 1000)


def test_owner_cloudflare_session_roundtrip_and_readonly_reopened_reads(enrolled):
    _, owner, store = enrolled
    session = store.create_cloudflare_session(CONFIG, IDENTITY)
    expected = store.cloudflare_session_for(session["cookie_token"], CONFIG, IDENTITY)
    assert expected["actor"] == ACTOR and expected["auth_provider"] == "cloudflare_access"
    assert expected["github_id"] is None
    with store._read_db() as db:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM auth_access_sessions")
    owner.close()
    # Each call opens/closes a new read-only connection; losing write ownership
    # does not silently upgrade a subsequent read or erase existing sessions.
    assert store.cloudflare_session_for(session["cookie_token"], CONFIG, IDENTITY) == expected
    assert store.cloudflare_session_for(session["cookie_token"], CONFIG, IDENTITY) == expected


@pytest.mark.parametrize("operation", ["create", "delete"])
def test_closed_owner_denies_cloudflare_mutations_without_schema_or_row_changes(enrolled, operation):
    path, owner, store = enrolled
    session = store.create_cloudflare_session(CONFIG, IDENTITY)
    before = schema(path)
    owner.close()
    action = (lambda: store.create_cloudflare_session(CONFIG, IDENTITY)) if operation == "create" else (
        lambda: store.delete_cloudflare_session(session["cookie_token"]))
    denied(action)
    assert schema(path) == before
    assert store.cloudflare_session_for(session["cookie_token"], CONFIG, IDENTITY)["actor"] == ACTOR
    with store._read_db() as db:
        assert db.execute("SELECT count(*) FROM auth_access_sessions").fetchone()[0] == 1


def test_owner_loss_after_additive_schema_creation_rolls_back_table(enrolled):
    path, owner, store = enrolled
    before = schema(path)
    calls = []

    def clock():
        calls.append(1)
        if len(calls) == 3:
            # Third call is inside the guarded transaction, after CREATE TABLE.
            owner.close()
        return 1000

    store.clock = clock
    denied(lambda: store.create_cloudflare_session(CONFIG, IDENTITY))
    assert len(calls) == 3
    assert schema(path) == before
    assert not any(name == "auth_access_sessions" for name, _ in before)


@pytest.mark.parametrize("operation", ["create", "delete"])
def test_standalone_cloudflare_store_refuses_late_enrollment(tmp_path, operation):
    path = tmp_path / "hub.sqlite"
    store = SessionStore(path, clock=lambda: 1000)
    session = store.create_cloudflare_session(CONFIG, IDENTITY)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE hub_pilot_owner (malformed TEXT)")
    before = schema(path)
    action = (lambda: store.create_cloudflare_session(CONFIG, IDENTITY)) if operation == "create" else (
        lambda: store.delete_cloudflare_session(session["cookie_token"]))
    denied(action)
    denied(lambda: SessionStore(path, clock=lambda: 1000))
    assert schema(path) == before
    assert store.cloudflare_session_for(session["cookie_token"], CONFIG, IDENTITY)["actor"] == ACTOR
    with store._read_db() as db:
        assert db.execute("SELECT count(*) FROM auth_access_sessions").fetchone()[0] == 1


def test_late_enrollment_denies_first_cloudflare_table_creation(tmp_path):
    path = tmp_path / "hub.sqlite"
    store = SessionStore(path, clock=lambda: 1000)
    with sqlite3.connect(path) as db:
        db.execute("CREATE VIEW hub_pilot_owner AS SELECT 1")
    before = schema(path)
    denied(lambda: store.create_cloudflare_session(CONFIG, IDENTITY))
    assert schema(path) == before
    assert not any(name == "auth_access_sessions" for name, _ in before)


def test_cloudflare_fixed_topic_reader_is_explicit_and_scoped():
    resolved = []

    def reader(principal, topic):
        resolved.append((principal, topic))
        return HumanRegistryAccess("fixture-readonly-service", frozenset({"engineering-hub"}))

    bridge = CanonicalRegistryBridge(object(), human_reader=reader)
    assert bridge.access(ACTOR, "engineering-hub", agent_bearer="never-borrow-this") == "fixture-readonly-service"
    with pytest.raises(RegistryAccessDenied):
        bridge.access(ACTOR, "other-topic", agent_bearer="never-borrow-this")
    assert resolved == [(ACTOR, "engineering-hub"), (ACTOR, "other-topic")]
    with pytest.raises(RegistrySourceUnavailable):
        CanonicalRegistryBridge(object()).access(ACTOR, "engineering-hub", agent_bearer="never-borrow-this")


@pytest.mark.parametrize("principal", ["human:cf:short", "human:cf:" + "G" * 64, "human:cf:" + "a" * 65])
def test_malformed_cloudflare_actor_never_invokes_reader(principal):
    def forbidden(*args):
        pytest.fail("Malformed human identity reached registry reader")

    with pytest.raises(RegistryAccessDenied):
        CanonicalRegistryBridge(object(), human_reader=forbidden).access(principal, "engineering-hub")
