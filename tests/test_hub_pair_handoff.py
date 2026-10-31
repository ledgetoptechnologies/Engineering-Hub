"""Real Unix sockets, synthetic supervisors; no Engine or host enrollment."""
import json
import os
import socket
import sqlite3
import struct
import sys
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux SCM_RIGHTS required')
pytest.importorskip('fcntl')
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hub_container_executor import pair_handoff as pair
from hub_container_executor.launch_supervisor import LaunchSupervisor
from hub_container_executor.launch_transport import UnixLaunchReceiptTransport
from tests.test_hub_executor_channel_handoff import enrollment


class Journal:
    instances = []
    def __init__(self, path, *, create):
        self.states = []; self.__class__.instances.append(self)
    def reserve(self, identity, data): self.states.append(('create_reserved', identity, data))
    def close(self): pass


class RoleJournal:
    def __init__(self): self.states = []
    def reserve_issuance(self, launch, nonce): self.states.append(('reserved', launch, nonce))
    def issuance_state(self, launch, state, digest): self.states.append((state, launch, digest))


class Peer:
    def __init__(self, sock, credentials, fail_confirmation=False, after_send=None):
        self.sock, self.credentials, self.fail_confirmation, self.after_send = sock, credentials, fail_confirmation, after_send
    def getsockopt(self, level, option, size): return struct.pack('=iII', *self.credentials)
    def sendmsg(self, *args):
        sent = self.sock.sendmsg(*args)
        if self.after_send: self.after_send()
        return sent
    def sendall(self, data):
        if self.fail_confirmation: raise OSError('synthetic confirmation failure')
        return self.sock.sendall(data)
    def __getattr__(self, name): return getattr(self.sock, name)


class Listener:
    def __init__(self, sock, credentials, fail_confirmation=False, after_send=None):
        self.sock, self.credentials, self.fail_confirmation, self.after_send = sock, credentials, fail_confirmation, after_send
    def settimeout(self, timeout): self.sock.settimeout(timeout)
    def accept(self):
        peer, address = self.sock.accept()
        return Peer(peer, self.credentials, self.fail_confirmation, self.after_send), address


@pytest.mark.skipif(not hasattr(os, 'geteuid') or os.geteuid() != 0, reason='root-owned journal fixture')
def test_pair_journal_cas_is_durable_and_never_recreated(tmp_path, monkeypatch):
    # A bounded tmpfs fixture is not accepted as operational local storage.
    import engineering_hub.pilot_ownership as ownership
    monkeypatch.setattr(ownership, '_local_filesystem', lambda fd: None)
    path = str(tmp_path / 'pair.db')
    identity = str(uuid4())
    journal = pair._Journal(path, create=True)
    try:
        journal.reserve(identity, {'manifest_digest': 'sha256:' + 'f' * 64})
        pair._transition(journal, identity, 'create_reserved', 'prepared', {'receipts': ['a', 'b']})
        with pytest.raises(pair.LaunchSupervisorError):
            pair._transition(journal, identity, 'create_reserved', 'committed', {})
        pair._transition(journal, identity, 'prepared', 'committed', {'commit': 'signed'})
    finally:
        journal.close()
    with sqlite3.connect(path) as db:
        state, data = db.execute('SELECT state,data FROM launch WHERE id=?', (identity,)).fetchone()
    assert state == 'committed' and json.loads(data) == {'commit': 'signed'}
    with pytest.raises(Exception):
        pair._Journal(path, create=True)


@pytest.mark.parametrize('failure', ['none', 'second_confirmation', 'commit_unknown', 'policy_changed', 'real_policy', 'real_policy_changed'])
def test_pair_issuer_commits_both_before_final_confirmation(tmp_path, monkeypatch, failure):
    # The root-owned journal boundary is separately implemented by _Journal;
    # this fixture records transitions while exercising real UDS + signatures.
    states = []
    if failure in {'real_policy', 'real_policy_changed'}:
        if not hasattr(os, 'geteuid') or os.geteuid() != 0:
            pytest.skip('root-owned protected file fixture')
        import engineering_hub.pilot_ownership as ownership
        monkeypatch.setattr(ownership, '_local_filesystem', lambda fd: None)
    else:
        monkeypatch.setattr(pair, '_Journal', Journal)
    def transition(journal, identity, old, new, data):
        states.append((old, new, identity, data))
        if new == 'committed' and failure == 'commit_unknown':
            raise OSError('synthetic ambiguous commit')
    if failure not in {'real_policy', 'real_policy_changed'}:
        monkeypatch.setattr(pair, '_transition', transition)
    keys = [Ed25519PrivateKey.generate() for _ in range(3)]
    releases = [enrollment(key) for key in keys[:2]]
    listeners, paths, supervisors, held = [], [], [], []
    boot, pair_id = str(uuid4()), str(uuid4())
    manifest = 'sha256:' + 'f' * 64
    for i, role in enumerate(('executor', 'coordinator')):
        path = str(tmp_path / (role + '.sock')); paths.append(path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path); listener.listen(1); listeners.append(listener)
        sup = LaunchSupervisor.__new__(LaunchSupervisor)
        sup._pid, sup._mutex = os.getpid(), threading.Lock()
        sup.profile = SimpleNamespace(role=None if i == 0 else 'coordinator', command_channel='inherited-seqpacket-v1')
        sup._issuance_open, sup._expires = True, time.monotonic() + 20
        sup._approval_expires_at_ms = int(time.time() * 1000) + 20000
        sup._consumer = SimpleNamespace(pid=101+i, start=11+i, boot=boot)
        sup._consumer_uid, sup._consumer_gid = 201+i, 301+i
        sup._container_id, sup._launch_id = ('e' if i == 0 else 'c') * 64, str(uuid4())
        sup._listener = Listener(listener, (101+i, 201+i, 301+i), failure == 'second_confirmation' and i == 1)
        sup._guard = lambda: None
        sup._validate_live = lambda deadline: None
        sup._handoff_manifest_digest = lambda: manifest
        sup._journal, sup._signer, sup.release = RoleJournal(), keys[i], releases[i]
        def hold(role=role, supervisor=sup):
            assert supervisor._mutex.locked()
            held.append(role)
        sup._hold = hold
        supervisors.append(sup)
    errors, output = [], []
    public = keys[2].public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    policy = {'schema': 'engineering-hub.pair-handoff-enrollment.v1', 'pair_id': pair_id,
        'pair_public_key': public.hex(), 'manifest_digest': manifest,
        'executor_launch_id': supervisors[0]._launch_id, 'coordinator_launch_id': supervisors[1]._launch_id,
        'executor_release_digest': pair._release_digest(releases[0]),
        'coordinator_release_digest': pair._release_digest(releases[1])}
    if failure == 'policy_changed':
        supervisors[1]._listener.after_send = lambda: policy.update(pair_public_key='0' * 64)
    raw_key = keys[2].private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                    serialization.NoEncryption())
    policy_file, key_file = str(tmp_path / 'policy.json'), str(tmp_path / 'signing.key')
    def fake_root_file(path, maximum):
        raw = pair._canonical(policy) if str(path) == policy_file else raw_key
        return raw, ((1, 2 if str(path) == policy_file else 3), 'sha256:' + __import__('hashlib').sha256(raw).hexdigest())
    if failure in {'real_policy', 'real_policy_changed'}:
        from pathlib import Path
        for path, raw in ((policy_file, pair._canonical(policy)), (key_file, raw_key)):
            Path(path).write_bytes(raw)
            os.chmod(path, 0o600)
        if failure == 'real_policy_changed':
            def replace_policy():
                replacement = Path(policy_file + '.new')
                replacement.write_bytes(pair._canonical(dict(policy, pair_public_key='0' * 64)))
                os.chmod(replacement, 0o600)
                os.replace(replacement, policy_file)
            supervisors[1]._listener.after_send = replace_policy
    else:
        monkeypatch.setattr(pair, '_root_file', fake_root_file)
    def issuer():
        try:
            pair.serve_pair(*supervisors, pair_id=pair_id, enrollment_file=policy_file, signing_key_file=key_file,
                            journal_file=str(tmp_path / 'pair.db'), timeout_seconds=5)
        except BaseException as error: errors.append(error)
    def receiver(i, role):
        transport = UnixLaunchReceiptTransport(socket_path=paths[i], expected_issuer_uid=os.getuid(),
                                               expected_issuer_gid=os.getgid())
        try:
            output.append(transport.issue_verified_pair_channel(role=role, pair_id=pair_id,
                manifest_digest=manifest, executor_launch_id=supervisors[0]._launch_id,
                coordinator_launch_id=supervisors[1]._launch_id, executor_enrollment=releases[0],
                coordinator_enrollment=releases[1], pair_authority_public_key=public,
                max_receipt_lifetime_seconds=30, timeout_ms=5000))
        except BaseException as error: errors.append(error)
        finally: transport.close()
    threads = [threading.Thread(target=issuer)] + [threading.Thread(target=receiver, args=(i, role))
               for i, role in enumerate(('executor', 'coordinator'))]
    try:
        for thread in threads: thread.start()
        for thread in threads: thread.join(7)
        assert all(not thread.is_alive() for thread in threads)
        if failure == 'second_confirmation':
            # The first confirmation may already have released its descriptor;
            # failure of the second is uncertainty, not an atomic rollback.
            assert len(errors) == 2 and len(output) == 1
            assert set(held) == {'executor', 'coordinator'}
        elif failure in {'commit_unknown', 'policy_changed', 'real_policy_changed'}:
            assert len(errors) == 3 and output == []
            assert set(held) == {'executor', 'coordinator'}
        else:
            assert not errors and len(output) == 2 and held == []
        if failure in {'real_policy', 'real_policy_changed'}:
            with sqlite3.connect(str(tmp_path / 'pair.db')) as db:
                state, raw = db.execute('SELECT state,data FROM launch WHERE id=?', (pair_id,)).fetchone()
            assert state == ('held' if failure == 'real_policy_changed' else 'committed')
            assert 'executor_receipt' in json.loads(raw)
        else:
            assert [row[1] for row in states] == (['prepared'] if failure == 'policy_changed' else ['prepared', 'committed'])
        assert all([entry[0] for entry in sup._journal.states] == ['reserved', 'sent'] for sup in supervisors)
    finally:
        for fd in output: os.close(fd)
        for listener in listeners: listener.close()
