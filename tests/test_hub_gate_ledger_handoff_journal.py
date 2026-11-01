"""Root-private durable reservation; identities and signatures are synthetic."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from engineering_hub import pilot_ownership
from hub_container_executor import launch_supervisor as launch
from tests.test_hub_container_executor_launch_supervisor import short_root


@pytest.fixture
def rig(short_root, monkeypatch):
    if os.geteuid() != 0: pytest.skip('requires disposable root-owned fixture')
    monkeypatch.setattr(pilot_ownership, '_local_filesystem', lambda _: None)
    journal = launch._Journal(str(short_root / 'journal'), create=True,
                              gate_mode=True, start_mode=True, ledger_mode=True)
    identity = str(uuid4())
    record = {'boot_id': str(uuid4()), 'container_id': 'a' * 64, 'pid': 123,
              'start_ticks': 456, 'manifest': 'sha256:' + 'b' * 64,
              'approval_expires_at_ms': 6000}
    binding = {'protocol': 'engineering-hub.gate-ledger-handoff.v1',
        'audience': 'engineering-hub.container-executor-ledger', 'nonce': 'c' * 64,
        'launch_id': identity, 'boot_id': record['boot_id'],
        'authenticated_host_pid': 123, 'process_start_ticks': 456,
        'container_id': record['container_id'], 'manifest_digest': 'sha256:' + 'd' * 64,
        'launch_profile_digest': record['manifest'], 'listener_device': 1,
        'listener_inode': 2, 'path_device': 3, 'path_inode': 4,
        'issued_at_ms': 1000, 'expires_at_ms': 2000}
    journal.reserve(identity, record)
    journal.state(identity, 'running', record)
    journal.reserve_issuance(identity, 'e' * 64)
    journal.issuance_state(identity, 'sent', 'f' * 64)
    try: yield journal, identity, record, binding
    finally: journal.close()


def test_durable_one_shot_and_reopen(rig):
    journal, identity, record, binding = rig
    assert journal.reserve_ledger_handoff(identity, record, binding, '1' * 64)
    with journal.transaction() as db:
        row = db.execute('SELECT supervisor_id,nonce,binding,digest FROM ledger_handoff').fetchone()
    assert row[:2] == (identity, binding['nonce']) and json.loads(row[2]) == binding and row[3] == '1' * 64
    with pytest.raises(Exception): journal.reserve_ledger_handoff(identity, record, binding, '1' * 64)
    path = str(journal.path); journal.close()
    reopened = launch._Journal(path, create=False, gate_mode=True, start_mode=True, ledger_mode=True)
    try:
        with pytest.raises(Exception): reopened.reserve_ledger_handoff(identity, record, binding, '1' * 64)
    finally: reopened.close()


@pytest.mark.parametrize('field,value', [
    ('authenticated_host_pid', True), ('process_start_ticks', 457), ('boot_id', str(uuid4())),
    ('container_id', '0' * 64), ('launch_profile_digest', 'sha256:' + '0' * 64),
    ('listener_inode', 0), ('path_device', True), ('expires_at_ms', 6001),
    ('audience', 'wrong'), ('nonce', 'bad')])
def test_invalid_binding_denies_and_latches(rig, field, value):
    journal, identity, record, binding = rig
    with pytest.raises(Exception): journal.reserve_ledger_handoff(identity, record, dict(binding, **{field: value}), '1' * 64)
    with pytest.raises(Exception): journal.reserve_ledger_handoff(identity, record, binding, '1' * 64)
    with journal.transaction() as db:
        assert db.execute('SELECT count(*) FROM ledger_handoff').fetchone() == (0,)


def test_exact_running_record_and_sent_receipt_required(rig):
    journal, identity, record, binding = rig
    journal.state(identity, 'held', record)
    with pytest.raises(Exception): journal.reserve_ledger_handoff(identity, record, binding, '1' * 64)
    with journal.transaction() as db:
        assert db.execute('SELECT count(*) FROM ledger_handoff').fetchone() == (0,)


def test_prior_receipt_must_be_sent(rig):
    journal, identity, record, binding = rig
    journal.issuance_state(identity, 'reserved', None)
    with pytest.raises(Exception): journal.reserve_ledger_handoff(identity, record, binding, '1' * 64)
    with journal.transaction() as db:
        assert db.execute('SELECT count(*) FROM ledger_handoff').fetchone() == (0,)


def test_running_record_must_match_exactly(rig):
    journal, identity, record, binding = rig
    with pytest.raises(Exception):
        journal.reserve_ledger_handoff(identity, dict(record, extra='changed'), binding, '1' * 64)
    with journal.transaction() as db:
        assert db.execute('SELECT count(*) FROM ledger_handoff').fetchone() == (0,)


def test_concurrent_requests_only_one_reservation(rig):
    journal, identity, record, binding = rig
    def invoke(nonce):
        try: return journal.reserve_ledger_handoff(identity, record, dict(binding, nonce=nonce), '1' * 64)
        except Exception: return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, ['a' * 64, 'b' * 64]))
    assert results.count(True) == 1
    with journal.transaction() as db:
        assert db.execute('SELECT count(*) FROM ledger_handoff').fetchone() == (1,)


def test_fsync_failure_after_commit_cannot_retry(rig, monkeypatch):
    journal, identity, record, binding = rig
    original = os.fsync
    def fail_parent(fd):
        if fd == journal.parent: raise OSError('synthetic fsync failure')
        return original(fd)
    with monkeypatch.context() as patch:
        patch.setattr(launch.os, 'fsync', fail_parent)
        with pytest.raises(Exception): journal.reserve_ledger_handoff(identity, record, binding, '1' * 64)
    with journal.transaction() as db:
        assert db.execute('SELECT count(*) FROM ledger_handoff').fetchone() == (1,)
    with pytest.raises(Exception): journal.reserve_ledger_handoff(identity, record, binding, '1' * 64)
    path = str(journal.path); journal.close()
    reopened = launch._Journal(path, create=False, gate_mode=True, start_mode=True, ledger_mode=True)
    try:
        with pytest.raises(Exception): reopened.reserve_ledger_handoff(identity, record, binding, '1' * 64)
    finally: reopened.close()


def test_ledger_mode_requires_start_mode(short_root, monkeypatch):
    if os.geteuid() != 0: pytest.skip('requires disposable root-owned fixture')
    monkeypatch.setattr(pilot_ownership, '_local_filesystem', lambda _: None)
    with pytest.raises(Exception):
        launch._Journal(str(short_root / 'invalid'), create=True, ledger_mode=True)
