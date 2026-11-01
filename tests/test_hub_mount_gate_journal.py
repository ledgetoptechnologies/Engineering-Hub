"""Root-private fixture journal only; external custody/fence are synthetic."""
from concurrent.futures import ThreadPoolExecutor
import os
from uuid import uuid4

import pytest

from engineering_hub import pilot_ownership
from hub_container_executor import launch_supervisor as launch
from hub_container_executor.mount_gate_protocol import IDENTITY_FIELDS
from hub_container_executor.mount_gate_controller import MountGateController
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tests.test_hub_container_executor_launch_supervisor import short_root
from tests.test_hub_mount_gate_protocol import fixture as decision_fixture
from tests.test_hub_mount_gate_controller import resources, observation


@pytest.fixture
def journal(short_root, monkeypatch):
    if os.geteuid() != 0:
        pytest.skip('requires isolated root-owned fixture')
    monkeypatch.setattr(pilot_ownership, '_local_filesystem', lambda _: None)
    value = launch._Journal(str(short_root / 'gate.sqlite3'), create=True, gate_mode=True)
    try: yield value
    finally: value.close()


def setup(journal):
    supervisor = str(uuid4()); record = {'container_id': 'a' * 64, 'pid': 123}
    journal.reserve(supervisor, {'profile_digest': 'synthetic'})
    journal.state(supervisor, 'running', record)
    decision = decision_fixture()
    expected = {k: decision[k] for k in IDENTITY_FIELDS}
    return supervisor, record, expected, decision


def test_one_use_across_concurrent_calls_and_reopen(journal):
    supervisor, record, expected, decision = setup(journal)
    def attempt():
        try: return journal.reserve_gate_issuance(supervisor, record, expected, decision, 'd' * 64)
        except Exception: return False
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: attempt(), range(2))) == [False, True]
    with journal.transaction() as db:
        assert db.execute('SELECT count(*) FROM gate_issuance').fetchone() == (1,)
    path = str(journal.path); journal.close()
    reopened = launch._Journal(path, create=False, gate_mode=True)
    try:
        with pytest.raises(RuntimeError):
            reopened.reserve_gate_issuance(supervisor, record, expected, decision, 'd' * 64)
        with reopened.transaction() as db:
            assert db.execute('SELECT count(*) FROM gate_issuance').fetchone() == (1,)
    finally: reopened.close()


@pytest.mark.parametrize('collision', ['operation_id', 'nonce', 'launch_id'])
def test_colliding_issuance_identity_is_rejected(journal, collision):
    supervisor, record, expected, decision = setup(journal)
    assert journal.reserve_gate_issuance(supervisor, record, expected, decision, 'd' * 64)
    other = decision_fixture()
    other['nonce'] = 'b' * 64
    other[collision] = decision[collision]
    with pytest.raises(Exception):
        journal.reserve_gate_issuance(supervisor, record, {k: other[k] for k in IDENTITY_FIELDS}, other, 'e' * 64)
    with journal.transaction() as db:
        assert db.execute('SELECT count(*) FROM gate_issuance').fetchone() == (1,)


def test_supervisor_cannot_issue_again_with_entirely_fresh_identities(journal):
    supervisor, record, expected, decision = setup(journal)
    assert journal.reserve_gate_issuance(supervisor, record, expected, decision, 'd' * 64)
    other = decision_fixture(); other['nonce'] = 'b' * 64
    assert all(other[key] != decision[key] for key in ('nonce', 'operation_id', 'launch_id'))
    with pytest.raises(Exception):
        journal.reserve_gate_issuance(supervisor, record, {k: other[k] for k in IDENTITY_FIELDS}, other, 'e' * 64)
    with journal.transaction() as db:
        assert db.execute('SELECT count(*) FROM gate_issuance').fetchone() == (1,)


@pytest.mark.parametrize('fault', ['record', 'enrollment', 'held'])
def test_running_record_and_full_enrollment_must_match(journal, fault):
    supervisor, record, expected, decision = setup(journal)
    if fault == 'record': record = dict(record, pid=124)
    if fault == 'enrollment': expected = dict(expected, fence=expected['fence'] + 1)
    if fault == 'held': journal.state(supervisor, 'held', record)
    with pytest.raises(RuntimeError):
        journal.reserve_gate_issuance(supervisor, record, expected, decision, 'd' * 64)
    with journal.transaction() as db:
        assert db.execute('SELECT count(*) FROM gate_issuance').fetchone() == (0,)


def test_commit_then_fsync_error_never_allows_reissue(journal, monkeypatch):
    supervisor, record, expected, decision = setup(journal)
    real = os.fsync
    def fail_parent(fd):
        if fd == journal.parent: raise OSError('synthetic late failure')
        return real(fd)
    with monkeypatch.context() as patcher:
        patcher.setattr(launch.os, 'fsync', fail_parent)
        with pytest.raises(OSError):
            journal.reserve_gate_issuance(supervisor, record, expected, decision, 'd' * 64)
    with journal.transaction() as db:
        assert db.execute('SELECT count(*) FROM gate_issuance').fetchone() == (1,)
    with pytest.raises(Exception):
        journal.reserve_gate_issuance(supervisor, record, expected, decision, 'd' * 64)
    other = decision_fixture()
    with pytest.raises(RuntimeError):
        journal.reserve_gate_issuance(supervisor, record, {k: other[k] for k in IDENTITY_FIELDS}, other, 'd' * 64)


def test_actual_controller_uses_durable_journal_before_send(journal, tmp_path):
    supervisor = str(uuid4()); record = {'container_id': 'a' * 64, 'pid': 123}
    journal.reserve(supervisor, {'profile_digest': 'synthetic'})
    journal.state(supervisor, 'running', record)
    client, server, peer, expected = resources(tmp_path)
    controller = MountGateController(channel=server, peer=peer, expected=expected,
        signing_key=Ed25519PrivateKey.generate(), verify_live=lambda _: True,
        reserve_issuance=lambda decision, digest: journal.reserve_gate_issuance(
            supervisor, record, expected, decision, digest),
        hold_uncertain=lambda _: None, timeout_seconds=2)
    try:
        client.send(observation(expected))
        assert controller.serve_once() == 'sent_unconfirmed'
        assert client.recv(8192)
        with journal.transaction() as db:
            row = db.execute('SELECT operation_id,nonce,gate_launch_id,supervisor_id FROM gate_issuance').fetchone()
        assert row == (expected['operation_id'], expected['nonce'], expected['launch_id'], supervisor)
    finally:
        client.close(); server.close(); peer.close()
