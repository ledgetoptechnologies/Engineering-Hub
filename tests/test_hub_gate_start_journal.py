"""Synthetic trusted authority; real journal and Unix seqpacket, no runtime launch."""
import json
import os
import socket
import select
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from engineering_hub import pilot_ownership
from hub_container_executor import application_gate_enrollment as enrollment
from hub_container_executor import launch_supervisor as launch
from hub_container_executor.mount_gate_protocol import IDENTITY_FIELDS
from hub_container_executor.mount_gate_start_enrollment import REQUEST, PROTOCOL, AUDIENCE
from tests.test_hub_application_gate_enrollment import vector, raw
from tests.test_hub_container_executor_launch_supervisor import (
    short_root, root_journal, root_file_constructor_fixture,
)
from tests.test_hub_mount_gate_controller import resources
from tests.test_hub_mount_gate_protocol import fixture as decision_fixture


@pytest.fixture
def rig(short_root, tmp_path, monkeypatch):
    if os.geteuid() != 0: pytest.skip('requires disposable root-owned fixture')
    monkeypatch.setattr(pilot_ownership, '_local_filesystem', lambda _: None)
    journal = launch._Journal(str(short_root / 'journal'), create=True, gate_mode=True, start_mode=True)
    manifest, image, supervisor, policy = vector()
    image = replace(image, image_config={'config': {'Entrypoint': list(enrollment.STATIC_ENTRYPOINT), 'Cmd': []}})
    signer = Ed25519PrivateKey.generate()
    policy.update(schema=enrollment.STATIC_SCHEMA,
        controller_public_key=signer.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex())
    policy_path = short_root / 'policy'; policy_path.write_bytes(raw(policy)); policy_path.chmod(0o600)
    key_path = short_root / 'key'; key_path.write_bytes(signer.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())); key_path.chmod(0o600)
    supervisor._pid, supervisor._mutex, supervisor._journal = os.getpid(), threading.Lock(), journal
    supervisor._launch_id = str(uuid4())
    record = {'container_id': 'a' * 64, 'pid': 123}
    supervisor._record = lambda: record
    supervisor._validate_live = lambda _: None
    supervisor._hold = lambda: journal.state(supervisor._launch_id, 'held', record)
    journal.reserve(supervisor._launch_id, {'fixture': True})
    journal.state(supervisor._launch_id, 'running', record)
    client, server, peer, expected = resources(tmp_path)
    kwargs = dict(channel=server, peer=peer,
        expected_without_nonce={k: v for k, v in expected.items() if k != 'nonce'},
        manifest=manifest, image=image, enrollment_file=str(policy_path), controller_key_file=str(key_path),
        verify_live=lambda _: True, timeout_seconds=2)
    try: yield journal, supervisor, client, expected, kwargs, record
    finally:
        client.close(); server.close(); peer.close(); journal.close()


def request(client, nonce):
    client.send(json.dumps({'protocol': REQUEST, 'nonce': nonce}, sort_keys=True, separators=(',', ':')).encode())


def decision_for(expected):
    decision = decision_fixture(); decision.update(expected)
    decision['observed_device'] = expected['expected_device']
    decision['observed_inode'] = expected['expected_inode']
    return decision


def start_payload(expected):
    return {'protocol': PROTOCOL, 'audience': AUDIENCE, 'identity': expected,
            'bootstrap_digest': 'sha256:' + '9' * 64, 'issued_at_ms': 1000, 'expires_at_ms': 2000}


def test_actual_supervisor_controller_persists_start_before_send_and_binds_later_decision(rig):
    journal, supervisor, client, expected, kwargs, record = rig
    request(client, expected['nonce'])
    result = supervisor.serve_one_gate_start_enrollment(**kwargs)
    assert result == {'identity': expected, 'delivery': 'sent_unconfirmed'}
    wire = client.recv(8192)
    with journal.transaction() as db:
        stored = db.execute('SELECT operation_id,nonce,gate_launch_id,supervisor_id,enrollment,digest FROM gate_start').fetchone()
    import hashlib
    assert stored[:4] == (expected['operation_id'], expected['nonce'], expected['launch_id'], supervisor._launch_id)
    assert json.loads(stored[4]) == json.loads(wire)['payload']
    assert stored[5] == hashlib.sha256(wire).hexdigest()
    decision = decision_for(expected)
    assert journal.reserve_gate_issuance(supervisor._launch_id, record, expected, decision, 'd' * 64)
    path = str(journal.path); journal.close()
    reopened = launch._Journal(path, create=False, gate_mode=True, start_mode=True)
    try:
        with pytest.raises(Exception):
            reopened.reserve_gate_start(supervisor._launch_id, record, kwargs['expected_without_nonce'],
                json.loads(wire)['payload']['bootstrap_digest'], json.loads(wire)['payload'], stored[5])
        with reopened.transaction() as db:
            assert db.execute('SELECT count(*) FROM gate_start').fetchone() == (1,)
    finally: reopened.close()


def test_later_decision_must_match_start_identity(rig):
    journal, supervisor, client, expected, kwargs, record = rig
    request(client, expected['nonce']); supervisor.serve_one_gate_start_enrollment(**kwargs)
    decision = decision_for(expected); decision['fence'] += 1
    with pytest.raises(Exception):
        journal.reserve_gate_issuance(supervisor._launch_id, record,
            {key: decision[key] for key in IDENTITY_FIELDS}, decision, 'd' * 64)


def test_lifecycle_lock_wait_is_bounded_and_no_reservation_is_written(rig):
    journal, supervisor, _client, _expected, kwargs, _record = rig
    supervisor._mutex.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(launch.LaunchSupervisorError):
            supervisor.serve_one_gate_start_enrollment(**{**kwargs, 'timeout_seconds': .05})
        assert time.monotonic() - started < 1
    finally:
        supervisor._mutex.release()
    with journal.transaction() as db:
        assert db.execute('SELECT count(*) FROM gate_start').fetchone() == (0,)


def test_recursive_callback_is_rejected_without_deadlock(rig):
    journal, supervisor, client, expected, kwargs, _record = rig
    def recursive(_pins):
        supervisor.serve_one_gate_start_enrollment(**kwargs)
        return True
    request(client, expected['nonce'])
    started = time.monotonic()
    with pytest.raises(launch.LaunchSupervisorError):
        supervisor.serve_one_gate_start_enrollment(**{**kwargs, 'verify_live': recursive})
    assert time.monotonic() - started < 1
    with journal.transaction() as db:
        assert db.execute('SELECT count(*) FROM gate_start').fetchone() == (0,)


def test_late_commit_failure_holds_and_cannot_issue_later_decision(rig, monkeypatch):
    journal, supervisor, client, expected, kwargs, record = rig
    request(client, expected['nonce'])
    fsync = os.fsync
    def fail_parent(fd):
        if fd == journal.parent: raise OSError('private injected failure')
        return fsync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(launch.os, 'fsync', fail_parent)
        with pytest.raises(launch.LaunchSupervisorError): supervisor.serve_one_gate_start_enrollment(**kwargs)
    with journal.transaction() as db:
        assert db.execute('SELECT count(*) FROM gate_start').fetchone() == (1,)
    decision = decision_for(expected)
    with pytest.raises(Exception):
        journal.reserve_gate_issuance(supervisor._launch_id, record, expected, decision, 'd' * 64)


def test_disconnect_after_durable_reservation_holds_and_never_reissues(rig):
    journal, supervisor, client, expected, kwargs, record = rig
    request(client, expected['nonce']); client.close()
    with pytest.raises(launch.LaunchSupervisorError): supervisor.serve_one_gate_start_enrollment(**kwargs)
    with journal.transaction() as db:
        assert db.execute('SELECT state FROM launch WHERE id=?', (supervisor._launch_id,)).fetchone() == ('held',)
        assert db.execute('SELECT count(*) FROM gate_start').fetchone() == (1,)
    decision = decision_for(expected)
    with pytest.raises(Exception):
        journal.reserve_gate_issuance(supervisor._launch_id, record, expected, decision, 'd' * 64)


def test_concurrent_start_reservations_are_single_use(rig):
    journal, supervisor, _client, expected, kwargs, record = rig
    payload = start_payload(expected)
    def attempt():
        try:
            return journal.reserve_gate_start(supervisor._launch_id, record, kwargs['expected_without_nonce'],
                payload['bootstrap_digest'], payload, 'e' * 64)
        except Exception:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == [False, True]
    with journal.transaction() as db:
        assert db.execute('SELECT count(*) FROM gate_start').fetchone() == (1,)


def test_sigkill_after_start_commit_preserves_replay_barrier(short_root, monkeypatch):
    if os.geteuid() != 0: pytest.skip('requires disposable root-owned fixture')
    monkeypatch.setattr(pilot_ownership, '_local_filesystem', lambda _: None)
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        try:
            os.close(read_fd)
            journal = launch._Journal(str(short_root / 'crash-start'), create=True, gate_mode=True, start_mode=True)
            supervisor = str(uuid4()); record = {'fixture': True}
            journal.reserve(supervisor, record); journal.state(supervisor, 'running', record)
            decision = decision_fixture(); expected = {key: decision[key] for key in IDENTITY_FIELDS}
            payload = start_payload(expected)
            journal.reserve_gate_start(supervisor, record, {k:v for k,v in expected.items() if k != 'nonce'},
                payload['bootstrap_digest'], payload, 'f' * 64)
            os.write(write_fd, b'C')
            threading.Event().wait(20)
        except BaseException:
            try: os.write(write_fd, b'E')
            except OSError: pass
        os._exit(1)
    os.close(write_fd)
    try:
        assert select.select([read_fd], [], [], 10)[0] and os.read(read_fd, 1) == b'C'
    finally:
        try:
            try: os.kill(child, signal.SIGKILL)
            except ProcessLookupError: pass
            deadline = time.monotonic() + 5
            status = None
            while time.monotonic() < deadline:
                pid, result = os.waitpid(child, os.WNOHANG)
                if pid == child:
                    status = result
                    break
                time.sleep(.01)
            assert status is not None, 'owned child did not exit'
        finally:
            os.close(read_fd)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
    reopened = launch._Journal(str(short_root / 'crash-start'), create=False, gate_mode=True, start_mode=True)
    try:
        with reopened.transaction() as db:
            supervisor, encoded = db.execute('SELECT supervisor_id,enrollment FROM gate_start').fetchone()
            state, record = db.execute('SELECT state,data FROM launch WHERE id=?', (supervisor,)).fetchone()
        payload = json.loads(encoded); expected = payload['identity']
        assert state == 'running'  # Original row is preserved, not evidence of a live owner.
        with pytest.raises(Exception):
            reopened.reserve_gate_start(supervisor, json.loads(record),
                {k:v for k,v in expected.items() if k != 'nonce'}, payload['bootstrap_digest'], payload, 'f' * 64)
    finally: reopened.close()


@pytest.mark.parametrize('enabled,version', [(False, 1), (True, 3)])
def test_real_constructor_selects_explicit_journal_mode_without_engine_effects(short_root, root_journal, enabled, version):
    with root_file_constructor_fixture(short_root, root_journal) as (_data, kwargs, requests):
        supervisor = launch.LaunchSupervisor(**kwargs, gate_start_mode=enabled)
        try:
            with supervisor._journal.transaction() as db:
                assert db.execute('PRAGMA user_version').fetchone() == (version,)
            assert supervisor._journal.start_mode is enabled
            assert requests == []
        finally:
            supervisor.close()


@pytest.mark.parametrize('bad', [None, 1, 'true'])
def test_constructor_denies_non_boolean_gate_mode_before_journal(short_root, root_journal, bad):
    with root_file_constructor_fixture(short_root, root_journal) as (_data, kwargs, requests):
        with pytest.raises(launch.LaunchSupervisorError):
            launch.LaunchSupervisor(**kwargs, gate_start_mode=bad)
        assert not os.path.exists(kwargs['journal_file']) and requests == []


def test_journal_denies_mixed_start_without_gate(short_root, monkeypatch):
    monkeypatch.setattr(pilot_ownership, '_local_filesystem', lambda _: None)
    with pytest.raises(launch.LaunchSupervisorError):
        launch._Journal(str(short_root / 'mixed'), create=True, gate_mode=False, start_mode=True)
    assert not (short_root / 'mixed').exists()
