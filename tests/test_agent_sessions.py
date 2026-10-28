"""Synthetic staging repository tests; raw fixture credentials are never printed."""
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import hashlib
import json
import sqlite3
import threading

import pytest

from agent_message_bus.agent_sessions import (
    AgentSessionError, AgentSessionRepository, MAX_ACTIVE_SESSIONS, MAX_BODY_BYTES,
    MAX_PENDING, MAX_SUBSCRIPTIONS, SessionPrincipal,
)


OWNER = 'human:42'


def check(condition, message):
    # Do not let pytest assertion rewriting include raw synthetic bearer values.
    if not condition:
        raise AssertionError(message)


def denied(code, operation):
    try:
        operation()
    except AgentSessionError as error:
        check(error.code == code, 'unexpected fixed error code')
        return error.status
    raise AssertionError('operation unexpectedly authorized')


class Clock:
    value = 1700000000.0
    def __call__(self):
        return self.value
    def advance(self, seconds):
        self.value += seconds


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    return AgentSessionRepository(tmp_path / 'sessions.sqlite3', OWNER, clock), clock


def installation(repo, name, family='codex', *, approve=True, lifetime=86400):
    # Deterministic non-live fixture secret with exactly32 bytes of entropy material.
    raw = hashlib.sha256(('synthetic-installation:' + name).encode()).digest()
    secret = 'ehi_' + base64.urlsafe_b64encode(raw).decode().rstrip('=')
    digest = hashlib.sha256(secret.encode()).hexdigest()
    row = repo.request_enrollment(family, name, 'Synthetic PC ' + name, digest)
    if approve:
        row = repo.approve(OWNER, row['id'], row['revision'], lifetime)
    return row, secret, digest


def pair(env):
    repo, clock = env
    a, asecret, _ = installation(repo, 'desktop')
    b, bsecret, _ = installation(repo, 'second-pc')
    return repo, clock, a, b, asecret, bsecret, repo.exchange(asecret), repo.exchange(bsecret)


def test_pairing_requires_owner_approval_and_cas(env):
    repo, clock = env
    row, secret, digest = installation(repo, 'pairing', approve=False)
    check(row['state'] == 'pending' and len(row['pairing_code']) == 6, 'pending pairing metadata missing')
    check(row['key_fingerprint'] == digest[:16], 'pairing fingerprint does not match')
    denied('installation_inactive', lambda: repo.exchange(secret))
    for actor in ('codex', 'human:43', 'human:cf:' + 'a' * 64):
        denied('session_owner_required', lambda: repo.approve(actor, row['id'], 1))
    denied('revision_conflict', lambda: repo.approve(OWNER, row['id'], True))
    approved = repo.approve(OWNER, row['id'], 1)
    check(approved['revision'] == 2 and approved['approved_by'] == OWNER, 'approval provenance missing')
    denied('revision_conflict', lambda: repo.rename(OWNER, row['id'], 1, 'New'))
    renamed = repo.rename(OWNER, row['id'], 2, 'New')
    check(renamed['revision'] == 3 and renamed['id'] == row['id'], 'rename changed identity or missed revision')
    denied('enrollment_not_pending', lambda: repo.approve(OWNER, row['id'], 3))
    check(repo.rename(OWNER, row['id'], 3, 'New')['revision'] == 3, 'no-op rename churned audit')


def test_pending_retry_is_bounded_and_expiry_does_not_self_approve(env):
    repo, clock = env
    row, secret, digest = installation(repo, 'pending-retry', approve=False)
    repeated = repo.request_enrollment('codex', 'pending-retry', 'Synthetic PC pending-retry', digest)
    check(repeated == row, 'enrollment retry changed deadline or identity')
    denied('enrollment_conflict', lambda: repo.request_enrollment('claude', 'pending-retry', 'Elsewhere', digest))
    clock.advance(600)
    check(repo.list_installations(OWNER)['installations'][0]['state'] == 'expired', 'pairing boundary must expire')
    denied('pairing_expired', lambda: repo.approve(OWNER, row['id'], 1))
    fresh = repo.request_enrollment('codex', 'pending-retry', 'Synthetic PC pending-retry', digest)
    check(fresh['id'] != row['id'] and fresh['state'] == 'pending', 'expired retry must require fresh approval')
    check(fresh['expires_at'] != row['expires_at'], 'fresh pairing must have a new deadline')
    denied('installation_inactive', lambda: repo.exchange(secret))


def test_pending_rate_and_global_pending_quota(env):
    repo, clock = env
    for n in range(20):
        installation(repo, 'rate-' + str(n), approve=False)
    denied('enrollment_limit', lambda: installation(repo, 'rate-overflow', approve=False))
    clock.advance(61)
    for n in range(20, MAX_PENDING):
        installation(repo, 'rate-' + str(n), approve=False)
    denied('enrollment_limit', lambda: installation(repo, 'pending-overflow', approve=False))
    clock.advance(600)
    installation(repo, 'new-after-expiry', approve=False)


def test_expired_pairing_reclamation_survives_historical_cap_and_keeps_audit(env):
    repo, clock = env
    # More than the lifetime installation cap, without ever holding more than
    # one unapproved request at once. Each old request retains its audit event.
    for n in range(260):
        row, _, _ = installation(repo, 'expired-' + str(n), approve=False)
        clock.advance(600)
    latest, _, _ = installation(repo, 'after-historical-cap', approve=False)
    with sqlite3.connect(repo.path) as db:
        count = db.execute('SELECT count(*) FROM session_installations').fetchone()[0]
        events = db.execute("SELECT count(*) FROM agent_session_audit WHERE action='enrollment.requested'").fetchone()[0]
    check(count == 1 and events == 261, 'pairing cleanup must reclaim rows without deleting audit')
    check(latest['state'] == 'pending', 'reclamation granted authority')


def test_pairing_cleanup_preserves_rejected_rate_window_and_approved_history(env):
    repo, clock, a, b, asecret, bsecret, sa, sb = pair(env)
    message = repo.send(sa['token'], b['id'], 'Synthetic retained history', 'history-key')
    repo.revoke_installation(OWNER, a['id'], a['revision'])
    rejected, _, _ = installation(repo, 'rejected-pairing', approve=False)
    repo.revoke_installation(OWNER, rejected['id'], rejected['revision'])
    for n in range(17):
        installation(repo, 'minute-' + str(n), approve=False)
    denied('enrollment_limit', lambda: installation(repo, 'minute-overflow', approve=False))
    check(any(r['id'] == rejected['id'] for r in repo.list_installations(OWNER)['installations']),
          'unexpired rejected pairing lost rate-window record')
    clock.advance(600)
    fresh, _, _ = installation(repo, 'trigger-cleanup', approve=False)
    rows = repo.list_installations(OWNER)['installations']
    check({r['id'] for r in rows} == {a['id'], b['id'], fresh['id']}, 'approved identity history removed')
    check(not any(r['id'] == rejected['id'] for r in rows), 'expired rejected pairing was not reclaimed')
    denied('enrollment_conflict', lambda: installation(repo, 'desktop'))
    denied('installation_inactive', lambda: repo.exchange(asecret))
    with sqlite3.connect(repo.path) as db:
        retained = db.execute('SELECT id FROM session_messages WHERE id=?', (message['id'],)).fetchone()
        sessions = db.execute('SELECT count(*) FROM agent_sessions').fetchone()[0]
        audit = db.execute('SELECT count(*) FROM agent_session_audit WHERE installation_id=?', (rejected['id'],)).fetchone()[0]
    check(retained is not None and sessions == 2 and audit == 2, 'cleanup changed durable approved history or audit')


def test_installation_exchange_and_session_authentication_are_distinct(env):
    repo, _ = env
    row, install_secret, _ = installation(repo, 'token-types')
    issued = repo.exchange(install_secret, 'Synthetic chat')
    principal = repo.authenticate_session(issued['token'])
    check(type(principal) is SessionPrincipal and principal.installation_id == row['id'], 'authenticated installation missing')
    check(principal.session_id == issued['session']['id'] and principal.family == 'codex', 'session principal changed family/identity')
    with pytest.raises(FrozenInstanceError):
        principal.family = 'hermes'
    denied('invalid_session', lambda: repo.authenticate_session(install_secret))
    denied('invalid_credential', lambda: repo.exchange(issued['token']))
    denied('invalid_session', lambda: repo.authenticate_session('codex'))
    denied('invalid_credential', lambda: repo.exchange('ehi_' + 'A' * 42))


def test_two_same_family_mailboxes_are_real_isolated_principals(env):
    repo, _, a, b, _, _, sa, sb = pair(env)
    message = repo.send(sa['token'], b['id'], 'private synthetic message', 'request-1')
    check(repo.messages(sa['token'])['messages'] == [], 'sender acquired recipient inbox')
    check(repo.messages(sb['token'])['messages'][0]['id'] == message['id'], 'recipient did not receive message')
    denied('message_not_found', lambda: repo.get(sa['token'], message['id']))
    denied('message_not_found', lambda: repo.claim(sa['token'], message['id']))
    peers = repo.peers(sa['token'])['installations']
    check(len(peers) == 1 and peers[0]['id'] == b['id'], 'same-family peer lost independent identity')
    answer = repo.send(sb['token'], a['id'], 'reply', 'request-1')
    check(repo.get(sa['token'], answer['id'])['sender'] == b['id'], 'same-family direct reply rejected or misattributed')
    denied('recipient_unavailable', lambda: repo.send(sa['token'], a['id'], 'self', 'self'))


def test_idempotency_survives_session_renewal_but_not_principal_boundary(env):
    repo, clock, a, b, asecret, _, sa, sb = pair(env)
    first = repo.send(sa['token'], b['id'], 'repeat', 'shared-key')
    second_session = repo.exchange(asecret)
    replay = repo.send(second_session['token'], b['id'], 'repeat', 'shared-key')
    check(first == replay, 'session renewal duplicated an accepted message')
    denied('idempotency_conflict', lambda: repo.send(second_session['token'], b['id'], 'changed', 'shared-key'))
    distinct = repo.send(sb['token'], a['id'], 'repeat', 'shared-key')
    check(distinct['id'] != first['id'], 'request key crossed installation boundary')
    check(len(repo.messages(sb['token'])['messages']) == 1, 'replay created a second row')


def test_claim_is_bound_to_session_handle_generation_and_live_lease(env):
    repo, clock, _, b, _, bsecret, sa, sb = pair(env)
    sibling = repo.exchange(bsecret)
    message = repo.send(sa['token'], b['id'], 'work', 'work')
    claim = repo.claim(sb['token'], message['id'])
    denied('claim_not_owned', lambda: repo.acknowledge(sibling['token'], message['id'], claim['claim_handle'], claim['generation']))
    denied('message_not_claimable', lambda: repo.claim(sibling['token'], message['id']))
    denied('claim_not_owned', lambda: repo.acknowledge(sb['token'], message['id'], 'ehc_' + 'A' * 43, claim['generation']))
    clock.advance(31)
    denied('claim_not_owned', lambda: repo.renew(sb['token'], message['id'], claim['claim_handle'], claim['generation']))
    replacement = repo.claim(sibling['token'], message['id'])
    check(replacement['generation'] == claim['generation'] + 1, 'reclaim did not fence old handle')
    denied('claim_not_owned', lambda: repo.acknowledge(sb['token'], message['id'], claim['claim_handle'], claim['generation']))
    done = repo.acknowledge(sibling['token'], message['id'], replacement['claim_handle'], replacement['generation'])
    check(done['status'] == 'acknowledged', 'current claim did not finish')
    denied('message_not_claimable', lambda: repo.claim(sb['token'], message['id']))


def test_renew_extends_only_current_owned_claim(env):
    repo, clock, _, b, _, _, sa, sb = pair(env)
    message = repo.send(sa['token'], b['id'], 'renew', 'renew')
    claim = repo.claim(sb['token'], message['id'])
    clock.advance(10)
    renewed = repo.renew(sb['token'], message['id'], claim['claim_handle'], claim['generation'])
    check(renewed['generation'] == claim['generation'], 'renew changed claim generation')
    clock.advance(21)
    check(repo.acknowledge(sb['token'], message['id'], renewed['claim_handle'], renewed['generation'])['status'] == 'acknowledged', 'renewal did not extend lease')


def test_session_revoke_keeps_sibling_installation_and_other_pc_alive(env):
    repo, _, _, b, _, bsecret, sa, sb = pair(env)
    sibling = repo.exchange(bsecret)
    message = repo.send(sa['token'], b['id'], 'revoked claimant', 'revoke')
    claim = repo.claim(sb['token'], message['id'])
    repo.revoke_session(OWNER, sb['session']['id'], 1)
    denied('invalid_session', lambda: repo.authenticate_session(sb['token']))
    denied('invalid_session', lambda: repo.acknowledge(sb['token'], message['id'], claim['claim_handle'], claim['generation']))
    check(repo.authenticate_session(sibling['token']).installation_id == b['id'], 'sibling revoked with one session')
    repo.authenticate_session(sa['token'])
    denied('message_not_claimable', lambda: repo.claim(sibling['token'], message['id']))
    denied('revision_conflict', lambda: repo.revoke_session(OWNER, sb['session']['id'], 1))


def test_installation_revoke_cascades_persists_and_does_not_clear_claim(env):
    repo, _, a, b, _, bsecret, sa, sb = pair(env)
    sibling = repo.exchange(bsecret)
    message = repo.send(sa['token'], b['id'], 'claim retained', 'cascade')
    repo.claim(sb['token'], message['id'])
    result = repo.revoke_installation(OWNER, b['id'], b['revision'])
    check(result['state'] == 'revoked', 'revocation metadata missing')
    restarted = AgentSessionRepository(repo.path, OWNER, repo.clock)
    for token in (sb['token'], sibling['token']):
        denied('invalid_session', lambda: restarted.authenticate_session(token))
    denied('installation_inactive', lambda: restarted.exchange(bsecret))
    check(restarted.authenticate_session(sa['token']).installation_id == a['id'], 'unrelated installation lost session')
    check(len(restarted.peers(sa['token'])['installations']) == 0, 'revoked installation remains targetable')
    with sqlite3.connect(repo.path) as db:
        state = db.execute('SELECT status FROM session_messages WHERE id=?', (message['id'],)).fetchone()[0]
    check(state == 'claimed', 'revocation implicitly reassigned work')


def test_expiry_boundaries_and_session_quota(env):
    repo, clock = env
    row, secret, _ = installation(repo, 'expiry', lifetime=900)
    sessions = [repo.exchange(secret) for _ in range(MAX_ACTIVE_SESSIONS)]
    denied('session_limit', lambda: repo.exchange(secret))
    clock.advance(900)
    denied('invalid_session', lambda: repo.authenticate_session(sessions[0]['token']))
    denied('installation_inactive', lambda: repo.exchange(secret))
    check(repo.list_installations(OWNER)['installations'][0]['state'] == 'expired', 'installation expires_at boundary admitted')
    check(all(s['state'] == 'expired' for s in repo.list_sessions(OWNER, row['id'])['sessions']), 'expired sessions shown active')


def test_session_expiry_blocks_all_operations_and_exchange_can_continue(env):
    repo, clock, _, b, asecret, _, sa, sb = pair(env)
    message = repo.send(sa['token'], b['id'], 'expire', 'expire')
    claim = repo.claim(sb['token'], message['id'])
    clock.advance(900)
    operations = [lambda: repo.messages(sb['token']), lambda: repo.get(sb['token'], message['id']),
                  lambda: repo.claim(sb['token'], message['id']), lambda: repo.peers(sb['token']),
                  lambda: repo.acknowledge(sb['token'], message['id'], claim['claim_handle'], claim['generation']),
                  lambda: repo.send(sa['token'], b['id'], 'another', 'new'),
                  lambda: repo.subscribe_session(sb['token'], lambda: None)]
    for operation in operations:
        denied('invalid_session', operation)
    repo.authenticate_session(repo.exchange(asecret)['token'])


def test_simultaneous_claims_have_one_winner_across_repository_connections(env):
    repo, _, _, b, _, bsecret, sa, sb = pair(env)
    sibling = repo.exchange(bsecret)
    other = AgentSessionRepository(repo.path, OWNER, repo.clock)
    message = repo.send(sa['token'], b['id'], 'race', 'race')
    barrier = threading.Barrier(2)
    def claim(store, token):
        barrier.wait()
        try:
            store.claim(token, message['id'])
            return 'won'
        except AgentSessionError as error:
            return error.code
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(claim, repo, sb['token']), pool.submit(claim, other, sibling['token'])]
        results = [future.result(timeout=10) for future in futures]
    check(sorted(results) == ['message_not_claimable', 'won'], 'claim race had multiple winners')


def test_concurrent_revoke_finish_is_serialized_and_never_finishes_after_revoke(env):
    repo, _, _, b, _, bsecret, sa, _ = pair(env)
    other = AgentSessionRepository(repo.path, OWNER, repo.clock)
    for n in range(6):
        session = repo.exchange(bsecret)
        message = repo.send(sa['token'], b['id'], 'atomic finish', 'atomic-' + str(n))
        claim = repo.claim(session['token'], message['id'])
        barrier = threading.Barrier(2)
        def finish():
            barrier.wait()
            try:
                repo.acknowledge(session['token'], message['id'], claim['claim_handle'], claim['generation'])
                return 'finished'
            except AgentSessionError as error:
                return error.code
        def revoke():
            barrier.wait()
            other.revoke_session(OWNER, session['session']['id'], 1)
        with ThreadPoolExecutor(2) as pool:
            finished, revoked = pool.submit(finish), pool.submit(revoke)
            result = finished.result(timeout=10); revoked.result(timeout=10)
        events = repo.audit(OWNER)['events']
        revoke_seq = next(e['id'] for e in events if e['action'] == 'session.revoked' and e['session_id'] == session['session']['id'])
        acks = [e['id'] for e in events if e['action'] == 'message.acknowledged' and e['message_id'] == message['id']]
        check(result in ('finished', 'invalid_session'), 'unexpected race result')
        check((len(acks) == 1 and acks[0] < revoke_seq) if result == 'finished' else not acks, 'finish committed after revocation')


def test_subscription_is_atomic_bounded_released_and_callback_failures_do_not_mask_revoke(env):
    repo, _ = env
    _, secret, _ = installation(repo, 'listeners')
    session = repo.exchange(secret)
    seen = []
    def notify():
        seen.append(denied('invalid_session', lambda: repo.authenticate_session(session['token'])))
    unsubs = [repo.subscribe_session(session['token'], notify)[1] for _ in range(MAX_SUBSCRIPTIONS)]
    denied('subscription_limit', lambda: repo.subscribe_session(session['token'], notify))
    unsubs[0](); unsubs[0]()
    def broken():
        seen.append('raised')
        raise RuntimeError('synthetic listener error')
    _, extra = repo.subscribe_session(session['token'], broken)
    result = repo.revoke_session(OWNER, session['session']['id'], 1)
    check(result['state'] == 'revoked' and len(seen) == MAX_SUBSCRIPTIONS, 'committed revocation lost callbacks or raised')
    for unsubscribe in unsubs:
        unsubscribe()
    extra()
    denied('invalid_session', lambda: repo.subscribe_session(session['token'], notify))


def test_subscribe_racing_revoke_cannot_leave_a_live_unauthorized_registration(env):
    repo, _ = env
    _, secret, _ = installation(repo, 'subscribe-race')
    for _ in range(6):
        session = repo.exchange(secret); seen = []; barrier = threading.Barrier(2)
        def subscribe():
            barrier.wait()
            try:
                _, unsubscribe = repo.subscribe_session(session['token'], lambda: seen.append(True))
                return unsubscribe
            except AgentSessionError as error:
                check(error.code == 'invalid_session', 'unexpected subscription error')
                return None
        def revoke():
            barrier.wait(); repo.revoke_session(OWNER, session['session']['id'], 1)
        with ThreadPoolExecutor(2) as pool:
            listener, revocation = pool.submit(subscribe), pool.submit(revoke)
            unsubscribe = listener.result(timeout=10); revocation.result(timeout=10)
        check(len(seen) == (1 if unsubscribe else 0), 'atomic subscription lost revoke notification')
        if unsubscribe:
            unsubscribe()


def test_metadata_audit_and_sqlite_store_have_no_raw_credentials_or_claim_handles(env):
    repo, _, a, b, asecret, bsecret, sa, sb = pair(env)
    message = repo.send(sa['token'], b['id'], 'body deliberately absent from audit', 'redaction')
    claim = repo.claim(sb['token'], message['id'])
    metadata = json.dumps([repo.list_installations(OWNER), repo.list_sessions(OWNER, b['id']),
                           repo.audit(OWNER), repo.peers(sa['token']), repo.get(sb['token'], message['id'])])
    contents = repo.path.read_bytes()
    for secret in (asecret, bsecret, sa['token'], sb['token'], claim['claim_handle']):
        check(secret not in metadata and secret.encode() not in contents, 'raw credential/handle persisted or exposed')
    check('body deliberately absent from audit' not in json.dumps(repo.audit(OWNER)), 'audit contains message body')
    for digest in (hashlib.sha256(asecret.encode()).hexdigest(), hashlib.sha256(bsecret.encode()).hexdigest()):
        check(digest not in metadata, 'full credential digest exposed')
    for operation in (lambda: repo.list_installations('codex'), lambda: repo.list_sessions('human:43', b['id']), lambda: repo.audit('hermes')):
        denied('session_owner_required', operation)


def test_text_and_pagination_limits_are_strict_and_private(env, monkeypatch):
    repo, _, a, b, _, _, sa, sb = pair(env)
    denied('message_too_large', lambda: repo.send(sa['token'], b['id'], 'x' * (MAX_BODY_BYTES + 1), 'large'))
    denied('invalid_input', lambda: repo.send(sa['token'], b['id'], '\x00', 'nul'))
    denied('invalid_input', lambda: repo.messages(sb['token'], after=True))
    denied('invalid_input', lambda: repo.messages(sb['token'], limit=101))
    for n in range(3):
        repo.send(sa['token'], b['id'], 'bounded ' + str(n), 'page-' + str(n))
    first = repo.messages(sb['token'], limit=2)
    second = repo.messages(sb['token'], after=first['next_cursor'], limit=2)
    check(len(first['messages']) == 2 and len(second['messages']) == 1, 'bounded pagination skipped or duplicated rows')
    import agent_message_bus.agent_sessions as core
    monkeypatch.setattr(core, 'MAX_MAILBOX', 3)
    denied('mailbox_limit', lambda: repo.send(sa['token'], b['id'], 'quota', 'quota'))


def test_configuration_owner_persistence_and_legacy_database_separation(tmp_path):
    path = tmp_path / 'state.sqlite3'
    AgentSessionRepository(path, OWNER)
    with pytest.raises(ValueError, match='session_owner_mismatch'):
        AgentSessionRepository(path, 'human:43')
    legacy = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(legacy) as db:
        db.execute('CREATE TABLE messages(id TEXT)')
    with pytest.raises(ValueError, match='separate_session_database_required'):
        AgentSessionRepository(legacy, OWNER)
    with pytest.raises(ValueError, match='explicit_session_owner_required'):
        AgentSessionRepository(tmp_path / 'uncreated.sqlite3', 'codex')


def test_clock_failures_are_closed(env):
    repo, clock = env
    _, secret, _ = installation(repo, 'clock')
    session = repo.exchange(secret)
    for invalid in (float('nan'), float('inf'), True, -1):
        clock.value = invalid
        denied('session_clock_unavailable', lambda: repo.authenticate_session(session['token']))


def test_pairing_codes_do_not_collide_between_pending_requests(env, monkeypatch):
    import agent_message_bus.agent_sessions as core
    repo, _ = env
    codes = iter((123456, 123456, 123457))
    monkeypatch.setattr(core.secrets, 'randbelow', lambda maximum: next(codes))
    first, _, _ = installation(repo, 'code-one', approve=False)
    second, _, _ = installation(repo, 'code-two', approve=False)
    check(first['pairing_code'] != second['pairing_code'], 'pending pairing codes collided')


def test_failed_cascade_rolls_back_parent_children_audit_and_notifications(env):
    repo, _ = env
    row, secret, _ = installation(repo, 'rollback')
    session = repo.exchange(secret)
    seen = []
    _, unsubscribe = repo.subscribe_session(session['token'], lambda: seen.append(True))
    with sqlite3.connect(repo.path) as db:
        db.execute("""CREATE TRIGGER synthetic_revoke_failure BEFORE UPDATE OF revoked_at ON agent_sessions
                   WHEN NEW.revoked_at IS NOT NULL BEGIN SELECT RAISE(ABORT,'synthetic rollback'); END""")
    with pytest.raises(sqlite3.IntegrityError, match='synthetic rollback'):
        repo.revoke_installation(OWNER, row['id'], row['revision'])
    check(not seen, 'rollback emitted a committed revocation callback')
    check(repo.authenticate_session(session['token']).installation_id == row['id'], 'rollback revoked child session')
    check(repo.list_installations(OWNER)['installations'][0]['state'] == 'active', 'rollback revoked parent installation')
    check(not any(e['action'] == 'installation.revoked' for e in repo.audit(OWNER)['events']), 'rollback retained revocation audit')
    with sqlite3.connect(repo.path) as db:
        db.execute('DROP TRIGGER synthetic_revoke_failure')
    repo.revoke_installation(OWNER, row['id'], row['revision'])
    check(seen == [True], 'successful commit did not invalidate the listener')
    unsubscribe()
