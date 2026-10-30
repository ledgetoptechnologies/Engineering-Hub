"""Private installation classification tests; synthetic credentials only."""
import base64
import hashlib

import pytest

from agent_message_bus.agent_installation_scope import InstallationScopeRepository
from agent_message_bus.agent_sessions import AgentSessionError, AgentSessionRepository

OWNER = 'human:42'


def check(condition, message):
    if not condition:
        raise AssertionError(message)


@pytest.fixture
def environment(tmp_path):
    clock = [1700000000.0]
    sessions = AgentSessionRepository(tmp_path / 'sessions.sqlite3', OWNER, lambda: clock[0])
    scopes = InstallationScopeRepository(sessions)
    return sessions, scopes, clock


def installation(sessions, name, approve=True):
    secret = 'ehi_' + base64.urlsafe_b64encode(hashlib.sha256(name.encode()).digest()).decode().rstrip('=')
    row = sessions.request_enrollment('codex', name, 'synthetic-host', hashlib.sha256(secret.encode()).hexdigest())
    if not approve:
        return row, None
    row = sessions.approve(OWNER, row['id'], row['revision'], until_revoked=True)
    return row, sessions.exchange(secret)['token']


def test_existing_approved_installation_has_no_implicit_internal_access(environment):
    sessions, scopes, _ = environment
    row, token = installation(sessions, 'ordinary')
    view = scopes.get(OWNER, row['id'])
    assert view['kind'] == 'unclassified' and view['revision'] == 0
    with pytest.raises(AgentSessionError, match='internal_installation_required'):
        scopes.require_internal(token)


def test_only_owner_can_classify_and_revision_prevents_lost_update(environment):
    sessions, scopes, _ = environment
    row, token = installation(sessions, 'owner-control')
    with pytest.raises(AgentSessionError, match='session_owner_required'):
        scopes.set('human:43', row['id'], 0, 'internal')
    result = scopes.set(OWNER, row['id'], 0, 'internal')
    assert result['internal_effective'] and result['revision'] == 1
    check(scopes.require_internal(token).installation_id == row['id'], 'internal session not admitted')
    with pytest.raises(AgentSessionError, match='scope_revision_conflict'):
        scopes.set(OWNER, row['id'], 0, 'project_only')


def test_removing_internal_classification_takes_effect_for_existing_session(environment):
    sessions, scopes, _ = environment
    row, token = installation(sessions, 'remove')
    scopes.set(OWNER, row['id'], 0, 'internal')
    scopes.require_internal(token)
    scopes.set(OWNER, row['id'], 1, 'project_only')
    with pytest.raises(AgentSessionError, match='internal_installation_required'):
        scopes.require_internal(token)


def test_classification_cannot_resurrect_revoked_installation(environment):
    sessions, scopes, _ = environment
    row, token = installation(sessions, 'revoke')
    scopes.set(OWNER, row['id'], 0, 'internal')
    sessions.revoke_installation(OWNER, row['id'], row['revision'])
    assert not scopes.get(OWNER, row['id'])['internal_effective']
    with pytest.raises(AgentSessionError, match='invalid_session'):
        scopes.require_internal(token)
    with pytest.raises(AgentSessionError, match='installation_inactive'):
        scopes.set(OWNER, row['id'], 1, 'internal')
    assert scopes.set(OWNER, row['id'], 1, 'unclassified')['kind'] == 'unclassified'


def test_session_expiry_is_not_extended_by_internal_classification(environment):
    sessions, scopes, clock = environment
    row, token = installation(sessions, 'expiry')
    scopes.set(OWNER, row['id'], 0, 'internal')
    clock[0] += 900
    with pytest.raises(AgentSessionError, match='invalid_session'):
        scopes.require_internal(token)


def test_pending_installations_cannot_gain_scope_rows(environment):
    sessions, scopes, _ = environment
    row, _ = installation(sessions, 'pending', approve=False)
    for kind in ('internal', 'project_only', 'unclassified'):
        with pytest.raises(AgentSessionError, match='installation_not_approved'):
            scopes.set(OWNER, row['id'], 0, kind)
    assert scopes.get(OWNER, row['id'])['revision'] == 0
    assert scopes.list(OWNER)['installations'] == []


def test_scope_persists_without_backfilling_other_installations(environment):
    sessions, scopes, _ = environment
    first, token = installation(sessions, 'first')
    second, _ = installation(sessions, 'second')
    scopes.set(OWNER, first['id'], 0, 'internal')
    reopened = InstallationScopeRepository(sessions)
    check(reopened.require_internal(token).installation_id == first['id'], 'classification lost on reopen')
    assert reopened.get(OWNER, second['id'])['kind'] == 'unclassified'
    listed = {row['installation_id']: row for row in reopened.list(OWNER)['installations']}
    assert listed[first['id']]['kind'] == 'internal' and listed[second['id']]['revision'] == 0


def test_bulk_conflict_rolls_back_prior_rows_and_audits(environment):
    sessions, scopes, _ = environment
    first, _ = installation(sessions, 'bulk-first')
    second, _ = installation(sessions, 'bulk-second')
    scopes.set(OWNER, second['id'], 0, 'project_only')
    before = sessions.audit(OWNER)
    with pytest.raises(AgentSessionError, match='scope_revision_conflict'):
        scopes.set_many(OWNER, [{'installation_id': first['id'], 'expected_revision': 0},
                              {'installation_id': second['id'], 'expected_revision': 0}], 'internal')
    assert scopes.get(OWNER, first['id'])['kind'] == 'unclassified'
    assert sessions.audit(OWNER) == before


def test_bulk_is_one_owner_action_with_independent_revisions(environment):
    sessions, scopes, _ = environment
    first, _ = installation(sessions, 'bulk-one')
    second, _ = installation(sessions, 'bulk-two')
    result = scopes.set_many(OWNER, [{'installation_id': first['id'], 'expected_revision': 0},
                                    {'installation_id': second['id'], 'expected_revision': 0}], 'internal')
    assert len(result['installations']) == 2
    assert all(row['kind'] == 'internal' and row['revision'] == 1 for row in result['installations'])
    with pytest.raises(AgentSessionError, match='invalid_installation_scope'):
        scopes.set_many(OWNER, [{'installation_id': first['id'], 'expected_revision': 1}] * 2, 'internal')
