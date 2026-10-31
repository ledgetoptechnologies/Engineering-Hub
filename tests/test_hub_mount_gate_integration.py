"""Real socket and two real journals; process/custodian evidence is synthetic.

This tests ordering and durable local one-use semantics, not atomicity across
the journals, host isolation, protected-filesystem acceptance or gate enablement.
"""
import os
import json
import select
import signal
import threading
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hub_container_executor import application_mount_gate as bootstrap
from hub_container_executor import ledger as ledger_module
from hub_container_executor import launch_supervisor as launch
from engineering_hub import pilot_ownership
from hub_container_executor.mount_gate_controller import MountGateController, MountGateControllerError
from hub_container_executor.mount_gate_protocol import IDENTITY_FIELDS
from tests.test_hub_application_mount_gate import TOKENS
from tests.test_hub_container_executor_ledger import open_ledger, gate_fixture
from tests.test_hub_mount_gate_controller import resources
from tests.test_hub_mount_gate_journal import journal
from tests.test_hub_container_executor_launch_supervisor import short_root


@pytest.mark.parametrize('outcome', ['consume_then_restart', 'issuance_commit_uncertain'])
def test_socket_controller_journal_ledger_reopen_is_fail_closed(journal, tmp_path, monkeypatch, outcome):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    monkeypatch.setattr(bootstrap, '_TARGET', str(tmp_path))
    ledger = open_ledger(tmp_path)
    command, _, ledger_pins = gate_fixture(ledger)
    client, server, peer, process_pins = resources(tmp_path)
    pins = dict(ledger_pins)
    for key in ('host_pid', 'process_start_ticks', 'boot_id', 'expected_device', 'expected_inode', 'mount_flags'):
        pins[key] = process_pins[key]
    ledger.reserve_mount_gate_challenge(pins)
    supervisor = str(uuid4()); record = {'container_id': 'a' * 64, 'pid': 123}
    journal.reserve(supervisor, {'profile_digest': 'synthetic'})
    journal.state(supervisor, 'running', record)
    key = Ed25519PrivateKey.generate()
    events, decisions, errors = [], [], []
    def reserve(decision, digest):
        decisions.append(decision)
        result = journal.reserve_gate_issuance(supervisor, record, pins, decision, digest)
        events.append('journal_committed')
        if outcome == 'issuance_commit_uncertain':
            raise OSError('synthetic loss after commit')
        return result
    controller = MountGateController(channel=server, peer=peer, expected=pins, signing_key=key,
        verify_live=lambda _: True, reserve_issuance=reserve,
        hold_uncertain=lambda _: events.append('uncertain'), timeout_seconds=5)
    class Executed(BaseException): pass
    monkeypatch.setattr(bootstrap.os, 'execve', lambda *_: (_ for _ in ()).throw(Executed()))
    def serve():
        try: controller.serve_once()
        except MountGateControllerError: errors.append('controller_failed')
        finally: server.close()
    worker = threading.Thread(target=serve); worker.start()
    try:
        if outcome == 'consume_then_restart':
            with pytest.raises(Executed):
                bootstrap.run_gate(channel=client, public_key=key.public_key(), expected=pins,
                    consume_challenge=ledger.consume_mount_gate_challenge, application_tokens=TOKENS, timeout_seconds=5)
        else:
            with pytest.raises(bootstrap.ApplicationMountGateError):
                bootstrap.run_gate(channel=client, public_key=key.public_key(), expected=pins,
                    consume_challenge=ledger.consume_mount_gate_challenge, application_tokens=TOKENS, timeout_seconds=5)
        worker.join(5)
        assert not worker.is_alive()
        assert events == (['journal_committed'] if outcome == 'consume_then_restart'
                          else ['journal_committed', 'uncertain'])
        assert errors == ([] if outcome == 'consume_then_restart' else ['controller_failed'])
        with journal.transaction() as db:
            assert db.execute('SELECT operation_id,nonce FROM gate_issuance').fetchone() == (pins['operation_id'], pins['nonce'])
        with ledger._connection() as db:
            expected_state = 'consumed' if outcome == 'consume_then_restart' else 'reserved'
            assert db.execute('SELECT state FROM mount_gate_challenges').fetchone()[0] == expected_state
        # Simulate loss of the existing owner: reopened ledger marks the running
        # command held and cannot consume, and the journal cannot issue again.
        ledger.close(); ledger = open_ledger(tmp_path)
        assert ledger.get(command.command_id).state == 'held'
        with pytest.raises(PermissionError): ledger.consume_mount_gate_challenge(decisions[0])
        path = str(journal.path); journal.close()
        reopened = launch._Journal(path, create=False, gate_mode=True)
        try:
            with pytest.raises(RuntimeError):
                reopened.reserve_gate_issuance(supervisor, record, pins, decisions[0], 'd' * 64)
        finally: reopened.close()
    finally:
        client.close(); server.close()
        worker.join(6)
        peer.close(); ledger.close()
        assert not worker.is_alive()


@pytest.mark.parametrize('boundary', ['before_send', 'after_consume'])
def test_sigkill_after_durable_boundary_remains_held(short_root, tmp_path, monkeypatch, boundary):
    if os.geteuid() != 0:
        pytest.skip('requires isolated root-owned fixture')
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    monkeypatch.setattr(pilot_ownership, '_local_filesystem', lambda _: None)
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        try:
            os.close(read_fd)
            ledger = open_ledger(tmp_path)
            _, _, ledger_pins = gate_fixture(ledger)
            client, server, peer, process_pins = resources(tmp_path)
            pins = dict(ledger_pins)
            for field in ('host_pid', 'process_start_ticks', 'boot_id', 'expected_device', 'expected_inode', 'mount_flags'):
                pins[field] = process_pins[field]
            ledger.reserve_mount_gate_challenge(pins)
            journal = launch._Journal(str(short_root / 'crash-gate.sqlite3'), create=True, gate_mode=True)
            supervisor = str(uuid4()); record = {'container_id': 'a' * 64, 'pid': 123}
            journal.reserve(supervisor, {'profile_digest': 'synthetic'}); journal.state(supervisor, 'running', record)
            def reserve(decision, digest):
                journal.reserve_gate_issuance(supervisor, record, pins, decision, digest)
                if boundary == 'before_send':
                    os.write(write_fd, b'C')  # Durable issuance exists; no response sent.
                    threading.Event().wait(20)
                return True
            controller = MountGateController(channel=server, peer=peer, expected=pins,
                signing_key=Ed25519PrivateKey.generate(), verify_live=lambda _: True,
                reserve_issuance=reserve, hold_uncertain=lambda _: None, timeout_seconds=5)
            if boundary == 'before_send':
                from tests.test_hub_mount_gate_controller import observation
                client.send(observation(pins)); controller.serve_once()
            else:
                bootstrap._TARGET = str(tmp_path)
                def blocked_exec(*_):
                    os.write(write_fd, b'X')  # Consumer committed; app not executed.
                    threading.Event().wait(20)
                bootstrap.os.execve = blocked_exec
                worker = threading.Thread(target=controller.serve_once); worker.start()
                bootstrap.run_gate(channel=client, public_key=controller._key.public_key(), expected=pins,
                    consume_challenge=ledger.consume_mount_gate_challenge, application_tokens=TOKENS, timeout_seconds=5)
        except BaseException:
            try: os.write(write_fd, b'E')
            except OSError: pass
        os._exit(1)
    os.close(write_fd)
    try:
        assert select.select([read_fd], [], [], 10)[0] and os.read(read_fd, 1) == (b'C' if boundary == 'before_send' else b'X')
    finally:
        os.kill(child, signal.SIGKILL)
        _, status = os.waitpid(child, 0)
        os.close(read_fd)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
    reopened = launch._Journal(str(short_root / 'crash-gate.sqlite3'), create=False, gate_mode=True)
    try:
        with reopened.transaction() as db:
            operation, nonce, supervisor, raw = db.execute(
                'SELECT operation_id,nonce,supervisor_id,decision FROM gate_issuance').fetchone()
            state, owner_record = db.execute('SELECT state,data FROM launch WHERE id=?', (supervisor,)).fetchone()
        decision = json.loads(raw); pins = {k: decision[k] for k in IDENTITY_FIELDS}
        assert operation == pins['operation_id'] and nonce == pins['nonce'] and state == 'running'
        with pytest.raises(RuntimeError):
            reopened.reserve_gate_issuance(supervisor, json.loads(owner_record), pins, decision, 'd' * 64)
    finally: reopened.close()
    ledger = open_ledger(tmp_path)
    try:
        with ledger._connection() as db:
            assert db.execute('SELECT state FROM mount_gate_challenges').fetchone()[0] == (
                'reserved' if boundary == 'before_send' else 'consumed')
            assert db.execute('SELECT state FROM commands').fetchone()[0] == 'held'
        with pytest.raises(PermissionError): ledger.consume_mount_gate_challenge(decision)
    finally: ledger.close()
