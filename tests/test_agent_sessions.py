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
    MAX_ALL_SESSIONS, MAX_PENDING, MAX_SESSION_HISTORY, MAX_SUBSCRIPTIONS,
    SESSION_HISTORY_WINDOW_SECONDS, SessionPrincipal,
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


def test_until_revoked_approval_survives_time_and_restart_but_sessions_expire(env):
    repo, clock = env
    row, secret, _ = installation(repo, 'persistent', approve=False)
    row = repo.approve(OWNER, row['id'], 1, until_revoked=True)
    check(row['expires_at'] is None and row['state'] == 'active', 'persistent approval not represented')
    check(any(e['action'] == 'installation.approved_until_revoked' for e in repo.audit(OWNER)['events']), 'persistent consent audit missing')
    issued = repo.exchange(secret)
    check(repo.authenticate_session(issued['token']).expires_at == clock.value + 900, 'session lost finite cap')
    clock.advance(900)
    denied('invalid_session', lambda: repo.authenticate_session(issued['token']))
    clock.advance(10 * 86400)
    reopened = AgentSessionRepository(repo.path, OWNER, clock)
    check(reopened.list_installations(OWNER)['installations'][0]['state'] == 'active', 'approval expired after restart')
    fresh = reopened.exchange(secret)
    observed = []
    principal, unsubscribe = reopened.subscribe_session(fresh['token'], lambda: observed.append(True))
    check(principal.expires_at == clock.value + 900, 'reconnect issued unbounded session')
    reopened.revoke_installation(OWNER, row['id'], row['revision'])
    check(observed == [True], 'revocation did not invalidate listener')
    denied('invalid_session', lambda: reopened.authenticate_session(fresh['token']))
    denied('installation_inactive', lambda: reopened.exchange(secret))
    unsubscribe()


def test_owner_converts_only_active_bounded_approvals_with_cas_and_audit(env):
    repo, clock = env
    row, secret, _ = installation(repo, 'convert', lifetime=60)
    pending, _, _ = installation(repo, 'still-pending', approve=False)
    denied('session_owner_required', lambda: repo.keep_approved('codex', row['id'], row['revision']))
    denied('installation_inactive', lambda: repo.keep_approved(OWNER, pending['id'], 1))
    denied('revision_conflict', lambda: repo.keep_approved(OWNER, row['id'], 1))
    converted = repo.keep_approved(OWNER, row['id'], row['revision'])
    check(converted['expires_at'] is None and converted['revision'] == row['revision'] + 1, 'conversion missing')
    check(repo.keep_approved(OWNER, row['id'], converted['revision']) == converted, 'conversion retry changed record')
    clock.advance(61)
    repo.exchange(secret)
    repo.revoke_installation(OWNER, row['id'], converted['revision'])
    denied('installation_inactive', lambda: repo.keep_approved(OWNER, row['id'], converted['revision'] + 1))
    expired, _, _ = installation(repo, 'expired-convert', lifetime=1)
    clock.advance(1)
    denied('installation_inactive', lambda: repo.keep_approved(OWNER, expired['id'], expired['revision']))
    check(any(event['action'] == 'installation.kept_approved' for event in repo.audit(OWNER)['events']), 'conversion audit missing')


def test_persistent_peers_and_pending_zero_sentinel_fail_closed(env):
    repo, clock = env
    row, _, _ = installation(repo, 'permanent-peer', approve=False)
    repo.approve(OWNER, row['id'], 1, until_revoked=True)
    _, secret, _ = installation(repo, 'observer', lifetime=604800)
    check(row['id'] in [p['id'] for p in repo.peers(repo.exchange(secret)['token'])['installations']], 'persistent peer omitted')
    pending, psecret, _ = installation(repo, 'zero-pending', approve=False)
    with repo._transaction() as db:
        db.execute('UPDATE session_installations SET expires_at=0 WHERE id=?', (pending['id'],))
    denied('installation_inactive', lambda: repo.exchange(psecret))
    denied('pairing_expired', lambda: repo.approve(OWNER, pending['id'], 1, until_revoked=True))


def test_existing_bounded_approval_is_not_extended_on_reopen(env):
    repo, clock = env
    row, secret, _ = installation(repo, 'bounded-existing', lifetime=60)
    reopened = AgentSessionRepository(repo.path, OWNER, clock)
    check(reopened.list_installations(OWNER)['installations'][0]['expires_at'] == row['expires_at'], 'old grant extended')
    clock.advance(60)
    denied('installation_inactive', lambda: reopened.exchange(secret))


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


def test_ack_retry_requires_original_live_session_handle_and_generation(env):
    repo, clock, _, b, _, bsecret, sa, sb = pair(env)
    sibling = repo.exchange(bsecret)
    message = repo.send(sa['token'], b['id'], 'ack retry', 'ack-retry')
    claim = repo.claim(sb['token'], message['id'], lease_seconds=2)
    accepted = repo.acknowledge(sb['token'], message['id'], claim['claim_handle'], claim['generation'])
    clock.advance(3)  # A committed acknowledgement remains retryable after claim expiry.
    reopened = AgentSessionRepository(repo.path, OWNER, repo.clock)
    replay = reopened.acknowledge(sb['token'], message['id'], claim['claim_handle'], claim['generation'])
    check(replay == accepted, 'ack retry did not return original message state')
    denied('claim_not_owned', lambda: reopened.acknowledge(sibling['token'], message['id'], claim['claim_handle'], claim['generation']))
    denied('claim_not_owned', lambda: reopened.acknowledge(sb['token'], message['id'], 'ehc_' + 'A' * 43, claim['generation']))
    denied('claim_not_owned', lambda: reopened.acknowledge(sb['token'], message['id'], claim['claim_handle'], claim['generation'] + 1))
    with sqlite3.connect(repo.path) as db:
        receipt = db.execute('SELECT session_id,generation,claim_hash FROM session_ack_receipts WHERE message_id=?', (message['id'],)).fetchone()
        count = db.execute("SELECT count(*) FROM agent_session_audit WHERE action='message.acknowledged' AND message_id=?", (message['id'],)).fetchone()[0]
    check(receipt[0] == sb['session']['id'] and receipt[1] == claim['generation'], 'receipt lost claim identity')
    check(receipt[2] == hashlib.sha256(claim['claim_handle'].encode()).hexdigest(), 'receipt did not store claim digest')
    check(count == 1, 'ack replay duplicated audit')
    reopened.revoke_session(OWNER, sb['session']['id'], 1)
    denied('invalid_session', lambda: reopened.acknowledge(sb['token'], message['id'], claim['claim_handle'], claim['generation']))


def test_legacy_ack_without_receipt_cannot_be_replayed(env):
    repo, _, _, b, _, _, sa, sb = pair(env)
    message = repo.send(sa['token'], b['id'], 'old ack', 'old-ack')
    claim = repo.claim(sb['token'], message['id'])
    repo.acknowledge(sb['token'], message['id'], claim['claim_handle'], claim['generation'])
    with sqlite3.connect(repo.path) as db:
        db.execute('DELETE FROM session_ack_receipts WHERE message_id=?', (message['id'],))
    denied('claim_not_owned', lambda: repo.acknowledge(sb['token'], message['id'], claim['claim_handle'], claim['generation']))


def test_concurrent_ack_retry_commits_one_receipt_and_audit(env):
    repo, _, _, b, _, _, sa, sb = pair(env)
    message = repo.send(sa['token'], b['id'], 'concurrent ack', 'concurrent-ack')
    claim = repo.claim(sb['token'], message['id'])
    barrier = threading.Barrier(2)
    second = AgentSessionRepository(repo.path, OWNER, repo.clock)

    def acknowledge(repository):
        barrier.wait()
        return repository.acknowledge(sb['token'], message['id'], claim['claim_handle'], claim['generation'])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(acknowledge, (repo, second)))
    check(results[0] == results[1] and results[0]['status'] == 'acknowledged', 'concurrent ACK changed result')
    with sqlite3.connect(repo.path) as db:
        count = db.execute("SELECT count(*) FROM agent_session_audit WHERE action='message.acknowledged' AND message_id=?", (message['id'],)).fetchone()[0]
        receipts = db.execute('SELECT count(*) FROM session_ack_receipts WHERE message_id=?', (message['id'],)).fetchone()[0]
    check(count == 1 and receipts == 1, 'concurrent ACK duplicated receipt or audit')


def test_ack_receipt_rolls_back_with_failed_message_update(env):
    repo, _, _, b, _, _, sa, sb = pair(env)
    message = repo.send(sa['token'], b['id'], 'rollback ack', 'rollback-ack')
    claim = repo.claim(sb['token'], message['id'])
    with sqlite3.connect(repo.path) as db:
        db.execute("CREATE TRIGGER reject_ack BEFORE UPDATE OF status ON session_messages WHEN NEW.status='acknowledged' BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        repo.acknowledge(sb['token'], message['id'], claim['claim_handle'], claim['generation'])
    with sqlite3.connect(repo.path) as db:
        count = db.execute('SELECT count(*) FROM session_ack_receipts WHERE message_id=?', (message['id'],)).fetchone()[0]
        db.execute('DROP TRIGGER reject_ack')
    check(count == 0 and repo.get(sb['token'], message['id'])['status'] == 'claimed', 'failed ACK committed partial receipt')
    check(repo.acknowledge(sb['token'], message['id'], claim['claim_handle'], claim['generation'])['status'] == 'acknowledged', 'claim could not be acknowledged after rollback')


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


def _seed_session(db, installation_id, suffix, issued_at, expires_at, revoked_at=None):
    token_hash = hashlib.sha256(('synthetic-session:' + suffix).encode()).hexdigest()
    db.execute('INSERT INTO agent_sessions VALUES(?,?,?,?,?,?,NULL,?,1)',
               ('synthetic-' + suffix, installation_id, token_hash, 'Synthetic', issued_at, expires_at, revoked_at))


def test_persistent_approval_reconnects_beyond_historical_session_cap(env):
    repo, clock = env
    row, secret, _ = installation(repo, 'many-reconnects', approve=False)
    approved = repo.approve(OWNER, row['id'], row['revision'], until_revoked=True)
    for _ in range(1000):
        issued = repo.exchange(secret)
        check(issued['session']['expires_at'] is not None, 'session was not finite')
        clock.advance(901)
    current = repo.list_installations(OWNER)['installations'][0]
    check(current['id'] == row['id'] and current['revision'] == approved['revision']
          and current['expires_at'] is None and current['state'] == 'active',
          'reconnect changed persistent approval')
    recent = repo.list_sessions(OWNER, row['id'])
    check(set(recent) == {'sessions'} and len(recent['sessions']) <= MAX_SESSION_HISTORY,
          'owner session history response shape or bound changed')
    with sqlite3.connect(repo.path) as db:
        check(db.execute('SELECT count(*) FROM agent_sessions WHERE installation_id=?', (row['id'],)).fetchone()[0] == 1000,
              'historical sessions were deleted')


def test_active_session_burst_cap_recovers_after_finite_sessions_expire(env):
    repo, clock = env
    row, secret, _ = installation(repo, 'active-burst', approve=False)
    repo.approve(OWNER, row['id'], row['revision'], until_revoked=True)
    sessions = [repo.exchange(secret) for _ in range(MAX_ACTIVE_SESSIONS)]
    denied('session_limit', lambda: repo.exchange(secret))
    clock.advance(900)
    fresh = repo.exchange(secret)
    check(fresh['session']['state'] == 'active' and len(sessions) == MAX_ACTIVE_SESSIONS,
          'active-session burst cap did not recover after finite expiry')


def test_session_history_quota_counts_recent_effective_end_and_recovers(env):
    repo, clock = env
    row, secret, _ = installation(repo, 'window-quota', lifetime=604800)
    now = clock.value
    with repo._transaction() as db:
        for i in range(MAX_SESSION_HISTORY - 1):
            _seed_session(db, row['id'], 'install-' + str(i), now - 20, now - 1, now - 1)
        # A revoked session whose expiry was older than the window remains old;
        # an early revocation ends a session even when its original expiry is future.
        _seed_session(db, row['id'], 'ancient-expiry', now - 3 * SESSION_HISTORY_WINDOW_SECONDS,
                      now - SESSION_HISTORY_WINDOW_SECONDS - 1, now)
        _seed_session(db, row['id'], 'early-revoke', now - 2, now + 3600,
                      now - SESSION_HISTORY_WINDOW_SECONDS - 1)
        # Late revocation remains in the window, while an end exactly at cutoff does not.
        _seed_session(db, row['id'], 'late-revoke', now - 10, now + 3600, now - 10)
        _seed_session(db, row['id'], 'exact-cutoff', now - 2 * SESSION_HISTORY_WINDOW_SECONDS,
                      now + 3600, now - SESSION_HISTORY_WINDOW_SECONDS)
    denied('session_limit', lambda: repo.exchange(secret))
    listed = repo.list_sessions(OWNER, row['id'])
    ids = {session['id'] for session in listed['sessions']}
    check(len(listed['sessions']) == MAX_SESSION_HISTORY and 'synthetic-late-revoke' in ids,
          'recent bounded owner history omitted an in-window ending')
    check(listed['sessions'][0]['id'] == 'synthetic-late-revoke',
          'owner history is not newest-first')
    check(not {'synthetic-ancient-expiry', 'synthetic-early-revoke', 'synthetic-exact-cutoff'} & ids,
          'owner history used an incorrect effective-end boundary')
    clock.advance(SESSION_HISTORY_WINDOW_SECONDS + 1)
    fresh = repo.exchange(secret)
    check(fresh['session']['state'] == 'active', 'per-installation quota did not recover after its window')


def test_global_recent_session_quota_recovers_without_pruning(env):
    repo, clock = env
    rows = [installation(repo, 'global-quota-' + str(i))[0] for i in range(16)]
    target, secret, _ = installation(repo, 'global-quota-target', lifetime=604800)
    now = clock.value
    with repo._transaction() as db:
        for row in rows:
            for i in range(MAX_SESSION_HISTORY):
                _seed_session(db, row['id'], 'global-' + row['id'] + '-' + str(i),
                              now - 20, now - 1, now - 1)
    with sqlite3.connect(repo.path) as db:
        check(db.execute('SELECT count(*) FROM agent_sessions').fetchone()[0] == MAX_ALL_SESSIONS,
              'synthetic global quota setup was not exact')
    denied('session_limit', lambda: repo.exchange(secret))
    clock.advance(SESSION_HISTORY_WINDOW_SECONDS + 1)
    fresh = repo.exchange(secret)
    check(fresh['session']['state'] == 'active', 'global quota did not recover after its window')
    with sqlite3.connect(repo.path) as db:
        check(db.execute('SELECT count(*) FROM agent_sessions').fetchone()[0] == MAX_ALL_SESSIONS + 1,
              'historical session rows were deleted')


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


def test_subscription_pools_are_bounded_fair_and_reclaimed_on_installation_revoke(env, monkeypatch):
    import agent_message_bus.agent_sessions as sessions_module
    monkeypatch.setattr(sessions_module, 'MAX_OWNER_SUBSCRIPTIONS', 3)
    monkeypatch.setattr(sessions_module, 'MAX_OWNER_SUBSCRIPTIONS_PER_SESSION', 2)
    monkeypatch.setattr(sessions_module, 'MAX_RESPONSE_SUBSCRIPTIONS', 1)
    repo, _ = env
    installation_row, secret, _ = installation(repo, 'reserved-pools')
    first, second = repo.exchange(secret), repo.exchange(secret)
    seen = []
    owner_a = repo.subscribe_session(first['token'], lambda: seen.append('a'), purpose='owner')[1]
    owner_b = repo.subscribe_session(first['token'], lambda: seen.append('b'), purpose='owner')[1]
    denied('subscription_limit', lambda: repo.subscribe_session(first['token'], lambda: None, purpose='owner'))
    owner_c = repo.subscribe_session(second['token'], lambda: seen.append('c'), purpose='owner')[1]
    def failing():
        seen.append('failed')
        raise RuntimeError('synthetic')
    response = repo.subscribe_session(first['token'], failing, purpose='response')[1]
    denied('subscription_limit', lambda: repo.subscribe_session(second['token'], lambda: None, purpose='response'))
    repo.revoke_installation(OWNER, installation_row['id'], installation_row['revision'])
    check(sorted(seen) == ['a', 'b', 'c', 'failed'], 'revocation did not notify every pool')
    for unsubscribe in (owner_a, owner_b, owner_c, response):
        unsubscribe(); unsubscribe()
    check(repo._listeners == {} and all(count == 0 for count in repo._listener_counts.values()),
          'revocation or repeated unsubscribe leaked pool capacity')


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
