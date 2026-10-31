"""Opt-in one-shot supervisor-to-owned-ledger transaction; not runtime wiring.

The caller must independently bind the public key to the approved supervisor,
authenticate the executor endpoint to the supervisor, and own/drain this call
before closing the ledger. A signed request does not prove host mount custody.
"""
import inspect
import array
import hmac
import json
import math
import os
import re
import select
import secrets
import socket
import threading
import time
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from .broker_seqpacket import PeerPin, _parse
from .ledger import SQLiteLedger
from .mount_gate_controller import _read_observation
from .mount_gate_protocol import IDENTITY_FIELDS, MAX_BYTES, _canonical, _payload
from .runtime_identity import LaunchReceiptIdentityVerifier
from engineering_hub.executor_launch_profile import GateCompletionPolicy, verify_profile_bytes

REQUEST = 'engineering-hub.gate-ledger-request.v1'
RESPONSE = 'engineering-hub.gate-ledger-response.v1'
DOMAIN = b'engineering-hub-gate-ledger-request-v1\x00'
_HEX = re.compile(r'[0-9a-f]{128}\Z')
# Linux generic socket ABI (the supported installed profiles are x86-64/arm64).
_SO_PASSPIDFD = 76
_SCM_PIDFD = 4


def _sender_packet(channel):
    """Receive the kernel-provided sender pidfd; quarantine every other FD."""
    received = []
    try:
        raw, ancillary, flags, _ = channel.recvmsg(MAX_BYTES + 1,
            socket.CMSG_SPACE(12) + socket.CMSG_SPACE(65 * array.array('i').itemsize),
            socket.MSG_CMSG_CLOEXEC)
        credentials, pidfds, invalid = [], [], False
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind in (socket.SCM_RIGHTS, _SCM_PIDFD):
                fds = array.array('i')
                fds.frombytes(data[:len(data) - len(data) % fds.itemsize])
                received.extend(fds)
                if kind == _SCM_PIDFD and len(data) == fds.itemsize and len(fds) == 1:
                    pidfds.extend(fds)
                else:
                    invalid = True
            elif level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(data) == 12:
                credentials.append(data)
            else:
                invalid = True
        if (invalid or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
                or not 0 < len(raw) <= MAX_BYTES or len(credentials) != 1 or len(pidfds) != 1):
            raise ValueError()
        fd = pidfds[0]
        if os.readlink('/proc/self/fd/' + str(fd)) != 'anon_inode:[pidfd]' or select.select([fd], [], [], 0)[0]:
            raise ValueError()
        received.remove(fd)
        return raw, fd, credentials[0]
    finally:
        for fd in received:
            try: os.close(fd)
            except OSError: pass


class GateLedgerChannelError(RuntimeError):
    def __init__(self):
        super().__init__('gate ledger channel unavailable')


class GateLedgerAcceptanceUnknown(GateLedgerChannelError):
    """Authenticated acceptance may have committed; only reconciliation is safe."""


def _sync(fn):
    return (callable(fn) and not inspect.iscoroutinefunction(fn)
            and not inspect.iscoroutinefunction(getattr(fn, '__call__', None)))


class GateLedgerWorker:
    """One exchange; the owner must retain and drain its execution thread.

    launch_verifier and hold_uncertain are independently trusted synchronous
    authority, not established by this constructor. The latter must hold the
    original operation when a transaction may have committed without delivery.
    A blocking ledger/callback is not cancellable by this class. The supervisor
    must separately authenticate this endpoint and its responses; the public key
    authenticates requests in the opposite direction only. Sender pidfd checks
    require independently enrolled direct, non-relayed host channel custody;
    kernel sender liveness alone does not prove the sender is the signing owner.
    """
    def __init__(self, *, channel, ledger, supervisor_public_key, launch_verifier,
                 manifest_digest, hold_uncertain, timeout_seconds, completion_policy=None,
                 require_sender_liveness=False, lookup_only=False, post_drain_only=False):
        try:
            if (type(channel) is not socket.socket or channel.family != socket.AF_UNIX
                    or channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET
                    or channel.getsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED) != 1
                    or type(ledger) is not SQLiteLedger or type(supervisor_public_key) is not bytes
                    or len(supervisor_public_key) != 32 or not _sync(getattr(launch_verifier, 'launch_binding', None))
                    or not _sync(hold_uncertain) or type(manifest_digest) is not str
                    or not re.fullmatch(r'sha256:[0-9a-f]{64}', manifest_digest)
                    or ledger._expected[0] != manifest_digest or type(timeout_seconds) not in (int, float)
                    or (completion_policy is not None and type(completion_policy) is not GateCompletionPolicy)
                    or type(require_sender_liveness) is not bool
                    or type(lookup_only) is not bool or type(post_drain_only) is not bool
                    or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 5):
                raise ValueError()
            channel.getpeername()
            self._channel, self._ledger = channel, ledger
            self._key = Ed25519PublicKey.from_public_bytes(supervisor_public_key)
            self._launch, self._manifest, self._hold = launch_verifier, manifest_digest, hold_uncertain
            self._completion_policy = completion_policy
            if require_sender_liveness:
                if not hasattr(socket, 'MSG_CMSG_CLOEXEC'): raise ValueError()
                channel.setsockopt(socket.SOL_SOCKET, _SO_PASSPIDFD, 1)
                if channel.getsockopt(socket.SOL_SOCKET, _SO_PASSPIDFD) != 1: raise ValueError()
            self._require_sender_liveness = require_sender_liveness
            self._lookup_only = lookup_only
            self._post_drain_only = post_drain_only
            self._timeout, self._pid, self._used, self._lock = timeout_seconds, os.getpid(), False, threading.Lock()
        except Exception:
            raise GateLedgerChannelError() from None

    def serve_once(self):
        if os.getpid() != self._pid: raise GateLedgerChannelError()
        with self._lock:
            if self._used: raise GateLedgerChannelError()
            self._used = True
        started = time.monotonic()
        deadline = started + self._timeout
        wall = int(time.time() * 1000)
        attempted = sent = False
        value = None
        sender_fd = None
        def scope():
            now = time.monotonic()
            if os.getpid() != self._pid or not started <= now < deadline: raise ValueError()
            if sender_fd is not None and select.select([sender_fd], [], [], 0)[0]: raise ValueError()
        try:
            binding = self._launch.launch_binding()
            if (type(binding) is not tuple or len(binding) != 5
                    or any(type(binding[i]) is not str for i in (0, 1, 4))
                    or any(type(binding[i]) is not int or binding[i] <= 0 for i in (2, 3))):
                raise ValueError()
            challenge = secrets.token_hex(32)
            request = _canonical({'protocol': 'engineering-hub.gate-ledger-challenge.v1', 'challenge': challenge})
            scope(); self._channel.settimeout(deadline - time.monotonic())
            if self._channel.send(request) != len(request): raise ValueError()
            scope(); self._channel.settimeout(deadline - time.monotonic())
            if self._require_sender_liveness:
                raw, sender_fd, sender_credentials = _sender_packet(self._channel)
            else:
                raw, credentials = _read_observation(self._channel)
                if len(credentials) != 1: raise ValueError()
            envelope = _parse(raw)
            if type(envelope) is not dict or set(envelope) != {'payload', 'signature'}: raise ValueError()
            payload, signature = envelope['payload'], envelope['signature']
            fields = {'protocol', 'challenge', 'launch_binding', 'manifest_digest', 'action',
                      'value', 'issued_at_ms', 'expires_at_ms'}
            if (type(payload) is not dict or set(payload) != fields or type(signature) is not str
                    or not _HEX.fullmatch(signature)): raise ValueError()
            self._key.verify(bytes.fromhex(signature), DOMAIN + _canonical(payload))
            issued, expires = payload['issued_at_ms'], payload['expires_at_ms']
            now = int(time.time() * 1000)
            if (payload['protocol'] != REQUEST or payload['challenge'] != challenge
                    or type(payload['launch_binding']) is not list or payload['launch_binding'] != list(binding)
                    or any(type(a) is not type(b) for a, b in zip(payload['launch_binding'], binding))
                    or payload['manifest_digest'] != self._manifest or payload['action'] not in ('reserve', 'consume', 'inspect_completion', 'inspect_completion_v4', 'accept_completion_v5', 'lookup_completion_v5', 'pending_gate_v5', 'health_ready_v5', 'pending_state_v1', 'seal_pair_drain_v1', 'pair_outcome_v1')
                    or type(issued) is not int or type(expires) is not int or not 0 < expires - issued <= 5000
                    or now < wall or issued < wall - 5000 or not issued <= now < expires
                    or expires <= wall or (time.monotonic() - started) * 1000 >= expires - wall): raise ValueError()
            value = payload['value']
            if type(value) is not dict: raise ValueError()
            is_accept = payload['action'] == 'accept_completion_v5'
            is_lookup = payload['action'] == 'lookup_completion_v5'
            is_pending = payload['action'] == 'pending_gate_v5'
            is_ready = payload['action'] == 'health_ready_v5'
            is_state = payload['action'] == 'pending_state_v1'
            is_pair_seal = payload['action'] == 'seal_pair_drain_v1'
            is_pair_outcome = payload['action'] == 'pair_outcome_v1'
            if self._post_drain_only and not is_pair_outcome: raise ValueError()
            if self._lookup_only and not is_lookup: raise ValueError()
            if (is_accept or is_lookup or is_pending or is_ready or is_state or is_pair_seal or is_pair_outcome) and (not self._require_sender_liveness or self._completion_policy is None):
                raise ValueError()
            is_snapshot = payload['action'] in ('inspect_completion', 'inspect_completion_v4')
            identity = value.get('identity') if (is_snapshot or is_accept or is_lookup or is_ready) else value
            if not (is_pending or is_state or is_pair_seal or is_pair_outcome) and (type(identity) is not dict or identity.get('manifest_digest') != self._manifest): raise ValueError()
            last_wall = [now]
            def fresh_decision():
                current = int(time.time() * 1000)
                decision_issued, decision_expires = value['issued_at_ms'], value['expires_at_ms']
                if (current < last_wall[0] or decision_issued < wall - 5000
                        or not decision_issued <= current < decision_expires or decision_expires <= wall
                        or (time.monotonic() - started) * 1000 >= decision_expires - wall):
                    raise ValueError()
                last_wall[0] = current
            def fresh_seal():
                scope()
                current = int(time.time() * 1000)
                if (self._launch.launch_binding() != binding or current < last_wall[0]
                        or not issued <= current < expires
                        or (time.monotonic() - started) * 1000 >= expires - wall):
                    raise ValueError()
                last_wall[0] = current
                return True
            if is_pair_seal or is_pair_outcome:
                if (set(value) != {'pair_id'} or type(value['pair_id']) is not str
                        or str(UUID(value['pair_id'])) != value['pair_id']): raise ValueError()
            elif is_state:
                if (set(value) != {'operation_id'} or type(value['operation_id']) is not str
                        or str(UUID(value['operation_id'])) != value['operation_id']): raise ValueError()
            elif is_ready:
                if (set(value) != {'identity', 'probe_digest'} or set(identity) != IDENTITY_FIELDS
                        or type(value['probe_digest']) is not str or not re.fullmatch(r'sha256:[0-9a-f]{64}', value['probe_digest'])): raise ValueError()
            elif is_pending:
                if set(value) != {'role'} or value['role'] not in ('candidate', 'rollback'): raise ValueError()
            elif is_accept:
                expected_fields = {'identity', 'role', 'executable_digest', 'health_contract',
                                   'probe_digest', 'health_receipt_digest', 'decision_payload_digest'}
                if (set(value) != expected_fields or set(identity) != IDENTITY_FIELDS
                        or self._completion_policy is None or value['role'] not in ('candidate', 'rollback')
                        or value['health_contract'] != self._completion_policy.health_contract
                        or value['executable_digest'] != getattr(self._completion_policy, value['role'] + '_executable_digest')
                        or any(type(value[k]) is not str or not re.fullmatch(r'sha256:[0-9a-f]{64}', value[k])
                               for k in ('probe_digest', 'health_receipt_digest', 'decision_payload_digest'))): raise ValueError()
            elif is_lookup:
                if set(value) != {'identity'} or set(identity) != IDENTITY_FIELDS: raise ValueError()
            elif is_snapshot:
                expected_fields = {'identity', 'probe_digest'}
                if payload['action'] == 'inspect_completion_v4':
                    expected_fields |= {'role', 'executable_digest', 'health_contract'}
                    if (self._completion_policy is None or value.get('role') not in ('candidate', 'rollback')
                            or value.get('health_contract') != self._completion_policy.health_contract
                            or value.get('executable_digest') != getattr(self._completion_policy,
                                value['role'] + '_executable_digest')): raise ValueError()
                if set(value) != expected_fields or set(identity) != IDENTITY_FIELDS: raise ValueError()
            elif payload['action'] == 'reserve':
                if set(value) != IDENTITY_FIELDS: raise ValueError()
            else:
                _payload(value)
                fresh_decision()
            scope()
            if self._launch.launch_binding() != binding: raise ValueError()
            current = int(time.time() * 1000)
            if (not last_wall[0] <= current < expires
                    or (time.monotonic() - started) * 1000 >= expires - wall): raise ValueError()
            last_wall[0] = current
            if payload['action'] == 'consume': fresh_decision()
            scope(); attempted = not (is_snapshot or is_lookup or is_pending or is_ready or is_state or is_pair_outcome)
            if payload['action'] == 'reserve':
                self._ledger.reserve_mount_gate_challenge(value)
            elif payload['action'] == 'consume' and self._ledger.consume_mount_gate_challenge(value) is not True:
                raise ValueError()
            elif is_accept:
                snapshot = self._ledger.accept_gate_completion(identity, **{k: value[k] for k in expected_fields - {'identity'}})
            elif is_lookup:
                snapshot = self._ledger.lookup_gate_completion(identity)
            elif is_pending:
                snapshot = self._ledger.pending_gate_context(role=value['role'])
            elif is_ready:
                snapshot = self._ledger.gate_health_ready(identity, value['probe_digest'])
            elif is_state:
                snapshot = self._ledger.pending_state_context(value['operation_id'])
            elif is_pair_seal:
                self._ledger.seal_pair_drain(value['pair_id'], binding, fresh_check=fresh_seal)
            elif is_pair_outcome:
                inventory = self._ledger.pair_drain_outcome(value['pair_id'], binding, fresh_check=fresh_seal)
                snapshot = ({'phase': 'draining'} if not self._post_drain_only else
                    {'phase': 'observed', 'inventory': inventory})
            elif is_snapshot:
                snapshot = self._ledger.gate_completion_snapshot(identity, value['probe_digest'])
                if payload['action'] == 'inspect_completion_v4' and snapshot['role'] != value['role']:
                    raise ValueError()
            scope()
            if payload['action'] == 'consume': fresh_decision()
            current = int(time.time() * 1000)
            if (self._launch.launch_binding() != binding or not last_wall[0] <= current < expires
                    or (time.monotonic() - started) * 1000 >= expires - wall):
                raise ValueError()
            response = _canonical({'protocol': RESPONSE, 'challenge': challenge,
                                   'action': payload['action'],
                                   **({'outcome': 'observed', 'snapshot': snapshot}
                                      if (is_snapshot or is_lookup or is_pending or is_ready or is_state or is_pair_outcome) else
                                      {'outcome': 'committed', 'snapshot': snapshot} if is_accept else {'outcome': 'committed'})})
            if len(response) > MAX_BYTES: raise ValueError()
            scope(); self._channel.settimeout(deadline - time.monotonic())
            if payload['action'] == 'consume': fresh_decision()
            if not last_wall[0] <= int(time.time() * 1000) < expires: raise ValueError()
            if self._channel.send(response) != len(response): raise ValueError()
            if is_pair_outcome and self._post_drain_only:
                scope(); self._channel.settimeout(deadline - time.monotonic())
                ack_fd = None
                try:
                    raw_ack, ack_fd, ack_credentials = _sender_packet(self._channel)
                    if (ack_credentials != sender_credentials
                            or raw_ack != _canonical({'protocol': 'engineering-hub.pair-outcome-ack.v1', 'challenge': challenge})):
                        raise ValueError()
                    scope()
                finally:
                    if ack_fd is not None: os.close(ack_fd)
            scope()
            sent = True
            return 'sent_unconfirmed'
        except Exception:
            if attempted and is_accept:
                raise GateLedgerAcceptanceUnknown() from None
            raise GateLedgerChannelError() from None
        finally:
            try:
                if attempted and not sent:
                    try: self._hold(json.loads(_canonical(identity if is_accept else value)))
                    except Exception: pass
            finally:
                if sender_fd is not None:
                    os.close(sender_fd)


class SupervisorLedgerClient:
    """One authenticated request to a separately pinned executor process.

    The caller independently provisions the key, live launch binding and peer
    mapping. A lost response is uncertain and must never be retried blindly.
    The channel and peer are borrowed; the owner closes them after this call.
    """
    def __init__(self, *, channel, peer, signing_key, launch_binding, manifest_digest, timeout_seconds,
                 lookup_only=False):
        try:
            if (type(channel) is not socket.socket or channel.family != socket.AF_UNIX
                    or channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET
                    or channel.getsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED) != 1
                    or type(peer) is not PeerPin or not isinstance(signing_key, Ed25519PrivateKey)
                    or type(launch_binding) is not tuple or len(launch_binding) != 5
                    or any(type(launch_binding[i]) is not str for i in (0, 1, 4))
                    or any(type(launch_binding[i]) is not int or launch_binding[i] <= 0 for i in (2, 3))
                    or type(manifest_digest) is not str or not re.fullmatch(r'sha256:[0-9a-f]{64}', manifest_digest)
                    or type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                    or type(lookup_only) is not bool
                    or not 0 < timeout_seconds <= 5): raise ValueError()
            channel.getpeername(); peer.check()
            self._channel, self._peer, self._key = channel, peer, signing_key
            self._binding, self._manifest, self._timeout = launch_binding, manifest_digest, timeout_seconds
            self._lookup_only = lookup_only
            self._outcome_ack = None
            self._pid, self._used, self._lock = os.getpid(), False, threading.Lock()
        except Exception:
            raise GateLedgerChannelError() from None


    def invoke(self, action, value, *, deadline_monotonic=None):
        if os.getpid() != self._pid: raise GateLedgerChannelError()
        with self._lock:
            if self._used: raise GateLedgerChannelError()
            self._used = True
        started = time.monotonic(); deadline = started + self._timeout
        if deadline_monotonic is not None:
            if (type(deadline_monotonic) not in (int, float) or not math.isfinite(deadline_monotonic)
                    or deadline_monotonic <= started): raise GateLedgerChannelError()
            deadline = min(deadline, deadline_monotonic)
        def scope():
            now = time.monotonic()
            if os.getpid() != self._pid or not started <= now < deadline: raise ValueError()
            self._peer.check()
        def receive():
            scope(); self._channel.settimeout(deadline - time.monotonic())
            raw, credentials = _read_observation(self._channel)
            if credentials != [(self._peer.pid, self._peer.uid, self._peer.gid)]: raise ValueError()
            scope(); return _parse(raw)
        try:
            if action not in ('reserve', 'consume', 'inspect_completion', 'inspect_completion_v4', 'accept_completion_v5', 'lookup_completion_v5', 'pending_gate_v5', 'health_ready_v5', 'pending_state_v1', 'seal_pair_drain_v1', 'pair_outcome_v1') or type(value) is not dict: raise ValueError()
            if self._lookup_only and action != 'lookup_completion_v5': raise ValueError()
            snapshot = json.loads(_canonical(value))
            challenge = receive()
            if (type(challenge) is not dict or set(challenge) != {'protocol', 'challenge'}
                    or challenge['protocol'] != 'engineering-hub.gate-ledger-challenge.v1'
                    or type(challenge['challenge']) is not str
                    or not re.fullmatch(r'[0-9a-f]{64}', challenge['challenge'])): raise ValueError()
            issued = int(time.time() * 1000); ttl = min(5000, int((deadline - time.monotonic()) * 1000))
            if issued <= 0 or ttl <= 0: raise ValueError()
            payload = {'protocol': REQUEST, 'challenge': challenge['challenge'], 'launch_binding': list(self._binding),
                       'manifest_digest': self._manifest, 'action': action, 'value': snapshot,
                       'issued_at_ms': issued, 'expires_at_ms': issued + ttl}
            raw = _canonical({'payload': payload, 'signature': self._key.sign(DOMAIN + _canonical(payload)).hex()})
            if len(raw) > MAX_BYTES: raise ValueError()
            scope(); self._channel.settimeout(deadline - time.monotonic())
            if self._channel.send(raw) != len(raw): raise ValueError()
            response = receive()
            is_snapshot = action in ('inspect_completion', 'inspect_completion_v4')
            is_accept = action == 'accept_completion_v5'
            is_lookup = action == 'lookup_completion_v5'
            is_pending = action == 'pending_gate_v5'
            is_ready = action == 'health_ready_v5'
            is_state = action == 'pending_state_v1'
            is_pair_outcome = action == 'pair_outcome_v1'
            expected = {'protocol': RESPONSE, 'challenge': challenge['challenge'], 'action': action,
                        'outcome': 'observed' if (is_snapshot or is_lookup or is_pending or is_ready or is_state or is_pair_outcome) else 'committed'}
            if is_pair_outcome:
                if (type(response) is not dict or set(response) != set(expected) | {'snapshot'}
                        or any(response[k] != v for k, v in expected.items())
                        or set(snapshot) != {'pair_id'} or str(UUID(snapshot['pair_id'])) != snapshot['pair_id']): raise ValueError()
                result = response['snapshot']
                if result == {'phase': 'draining'}:
                    result = None
                elif type(result) is dict and set(result) == {'phase', 'inventory'} and result['phase'] == 'observed':
                    result = result['inventory']
                else: raise ValueError()
                if result is not None and (type(result) is not dict or set(result) != {'commands', 'runs', 'targets', 'operations', 'invalid_receipts'}
                        or type(result['invalid_receipts']) is not int or result['invalid_receipts'] < 0
                        or any(type(result[name]) is not dict or len(result[name]) > 64 or any(type(k) is not str or type(n) is not int or n <= 0 for k, n in result[name].items()) for name in ('commands', 'runs', 'targets', 'operations'))): raise ValueError()
            elif is_state:
                if (type(response) is not dict or set(response) != set(expected) | {'snapshot'}
                        or any(response[k] != v for k, v in expected.items())
                        or set(snapshot) != {'operation_id'}): raise ValueError()
                record = response['snapshot']
                fields = {'operation_id', 'command_id', 'run_id', 'target_id', 'fencing_generation', 'manifest_digest', 'role'}
                if (type(record) is not dict or set(record) != fields
                        or record['operation_id'] != snapshot['operation_id']
                        or record['manifest_digest'] != self._manifest
                        or record['target_id'] != 'engineering-hub:staging'
                        or record['role'] not in ('candidate', 'rollback')
                        or type(record['fencing_generation']) is not int
                        or not 0 < record['fencing_generation'] < 2**63
                        or any(type(record[k]) is not str or str(UUID(record[k])) != record[k]
                               for k in ('operation_id', 'command_id', 'run_id'))): raise ValueError()
            elif is_ready:
                if (type(response) is not dict or set(response) != set(expected) | {'snapshot'}
                        or any(response[k] != v for k, v in expected.items())
                        or type(response['snapshot']) is not dict or set(response['snapshot']) != {'ready'}
                        or type(response['snapshot']['ready']) is not bool): raise ValueError()
            elif is_pending:
                if (type(response) is not dict or set(response) != set(expected) | {'snapshot'}
                        or any(response[k] != v for k, v in expected.items())
                        or set(snapshot) != {'role'} or snapshot['role'] not in ('candidate', 'rollback')): raise ValueError()
                record = response['snapshot']
                if record is not None:
                    fields = {'operation_id', 'run_id', 'fence', 'role', 'generation_id', 'container_id',
                              'image_config_id', 'runnable_digest', 'platform_manifest_digest'}
                    if (type(record) is not dict or set(record) != fields or record['role'] != snapshot['role']
                            or type(record['fence']) is not int or not 0 < record['fence'] < 2**63
                            or type(record['generation_id']) is not str
                            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', record['generation_id'])
                            or type(record['container_id']) is not str or not re.fullmatch(r'[0-9a-f]{64}', record['container_id'])
                            or any(type(record[k]) is not str or not re.fullmatch(r'sha256:[0-9a-f]{64}', record[k])
                                   for k in ('image_config_id', 'runnable_digest', 'platform_manifest_digest'))
                            or any(type(record[k]) is not str or str(UUID(record[k])) != record[k]
                                   for k in ('operation_id', 'run_id'))): raise ValueError()
            elif is_accept or is_lookup:
                if (type(response) is not dict or set(response) != set(expected) | {'snapshot'}
                        or any(response[k] != v for k, v in expected.items())): raise ValueError()
                record = response['snapshot']
                if is_accept:
                    if type(record) is not dict or _canonical(record) != _canonical(snapshot): raise ValueError()
                elif record is not None:
                    fields = {'identity', 'role', 'executable_digest', 'health_contract', 'probe_digest',
                              'health_receipt_digest', 'decision_payload_digest'}
                    if (type(record) is not dict or set(record) != fields or type(snapshot.get('identity')) is not dict
                            or _canonical(record['identity']) != _canonical(snapshot['identity'])
                            or record['role'] not in ('candidate', 'rollback')
                            or record['health_contract'] != 'engineering-hub.health-json-build-commit.v1'
                            or any(type(record[k]) is not str or not re.fullmatch(r'sha256:[0-9a-f]{64}', record[k])
                                   for k in ('executable_digest', 'probe_digest', 'health_receipt_digest', 'decision_payload_digest'))): raise ValueError()
            elif is_snapshot:
                if (type(response) is not dict or set(response) != set(expected) | {'snapshot'}
                        or any(response[k] != v for k, v in expected.items())
                        or type(response['snapshot']) is not dict
                        or set(response['snapshot']) != {'operation_id', 'health_operation_id',
                            'health_receipt_digest', 'decision_payload_digest', 'probe_digest', 'role',
                            'health_daemon_pid', 'health_started_at_ms', 'health_finished_at_ms', 'health_elapsed_ms'}): raise ValueError()
                observed = response['snapshot']
                if (type(snapshot.get('identity')) is not dict
                        or observed['operation_id'] != snapshot['identity'].get('operation_id')
                        or observed['probe_digest'] != snapshot.get('probe_digest')
                        or observed['role'] not in ('candidate', 'rollback')
                        or (action == 'inspect_completion_v4' and observed['role'] != snapshot.get('role'))
                        or any(type(observed[k]) is not int for k in ('health_daemon_pid', 'health_started_at_ms',
                                                                      'health_finished_at_ms', 'health_elapsed_ms'))
                        or observed['health_daemon_pid'] != snapshot['identity'].get('host_pid')
                        or not 0 < observed['health_started_at_ms'] <= observed['health_finished_at_ms']
                        or not 0 <= observed['health_elapsed_ms'] <= 120000
                        or not observed['health_finished_at_ms'] <= int(time.time() * 1000) < observed['health_finished_at_ms'] + 5000
                        or type(observed['health_operation_id']) is not str
                        or str(UUID(observed['health_operation_id'])) != observed['health_operation_id']
                        or any(type(observed[k]) is not str or not re.fullmatch(r'sha256:[0-9a-f]{64}', observed[k])
                               for k in ('health_receipt_digest', 'decision_payload_digest', 'probe_digest'))): raise ValueError()
            elif response != expected: raise ValueError()
            if not issued <= int(time.time() * 1000) < issued + ttl: raise ValueError()
            scope()
            if is_pair_outcome and result is not None:
                self._outcome_ack = (challenge['challenge'], deadline)
            return result if is_pair_outcome else response['snapshot'] if (is_snapshot or is_accept or is_lookup or is_pending or is_ready or is_state) else True
        except Exception:
            raise GateLedgerChannelError() from None

    def confirm_pair_outcome(self):
        if self._outcome_ack is None: return
        try:
            challenge, deadline = self._outcome_ack
            self._outcome_ack = None
            self._peer.check()
            self._channel.settimeout(deadline - time.monotonic())
            raw = _canonical({'protocol': 'engineering-hub.pair-outcome-ack.v1', 'challenge': challenge})
            if self._channel.send(raw) != len(raw): raise ValueError()
        except Exception:
            raise GateLedgerChannelError() from None


class OwnedGateLedgerService:
    """Own a pre-enrolled listener and drain every admitted transaction on close.

    The factory must independently authenticate listener custody, public key,
    launch verifier and hold callback. This type check is not enrollment. A
    failed exchange makes the service fail closed. Never force-cancel a ledger
    commit: close waits for the worker thread, even after a deadline expires.
    """
    def __init__(self, *, listener, ledger, supervisor_public_key, launch_verifier,
                 manifest_digest, hold_uncertain, timeout_seconds, completion_profile_bytes=None,
                 require_sender_liveness=False):
        try:
            if (type(listener) is not socket.socket or listener.family != socket.AF_UNIX
                    or listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET
                    or listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1
                    or type(ledger) is not SQLiteLedger or type(supervisor_public_key) is not bytes
                    or len(supervisor_public_key) != 32 or type(launch_verifier) is not LaunchReceiptIdentityVerifier
                    or not _sync(hold_uncertain)
                    or type(manifest_digest) is not str or not re.fullmatch(r'sha256:[0-9a-f]{64}', manifest_digest)
                    or ledger._expected[0] != manifest_digest or type(timeout_seconds) not in (int, float)
                    or type(require_sender_liveness) is not bool
                    or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 5): raise ValueError()
            self.ledger = ledger
            if not hmac.compare_digest(launch_verifier.issuer_public_key(), supervisor_public_key):
                raise ValueError()
            completion_policy = None
            if completion_profile_bytes is not None:
                profile = verify_profile_bytes(completion_profile_bytes, launch_verifier.launch_profile_digest())
                if profile.gate_completion is None: raise ValueError()
                if require_sender_liveness and profile.gate_ledger_channel != 'supervisor-listener-v1': raise ValueError()
                completion_policy = profile.gate_completion
            self._listener, self._key, self._launch = listener, supervisor_public_key, launch_verifier
            self._manifest, self._hold, self._timeout, self._on_failure = manifest_digest, hold_uncertain, timeout_seconds, None
            self._completion_policy = completion_policy
            self._require_sender_liveness = require_sender_liveness
            self._lookup_only = False
            self._phase_lock, self._post_drain, self._outcome_sent = threading.Lock(), False, threading.Event()
            self._pid, self._lock, self._stopping, self._thread, self._failed = os.getpid(), threading.Lock(), False, None, False
            listener.settimeout(.1)
        except Exception:
            raise GateLedgerChannelError() from None

    def start(self, *, on_failure):
        try:
            if os.getpid() != self._pid: raise ValueError()
            with self._lock:
                if (os.getpid() != self._pid or self._stopping or self._thread is not None
                        or not _sync(on_failure)): raise ValueError()
                self._on_failure = on_failure
                thread = threading.Thread(target=self._run, name='hub-gate-ledger', daemon=False)
                thread.start()
                self._thread = thread
        except Exception:
            raise GateLedgerChannelError() from None

    def _run(self):
        failed = False
        try:
            while True:
                with self._lock:
                    if self._stopping: break
                try: channel, _ = self._listener.accept()
                except socket.timeout: continue
                with channel:
                    with self._lock:
                        if self._stopping: break
                    channel.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
                    with self._phase_lock:
                        if self._post_drain and self._outcome_sent.is_set(): raise GateLedgerChannelError()
                        worker = GateLedgerWorker(channel=channel, ledger=self.ledger, supervisor_public_key=self._key,
                            launch_verifier=self._launch, manifest_digest=self._manifest, hold_uncertain=self._hold,
                            timeout_seconds=self._timeout, completion_policy=self._completion_policy,
                            require_sender_liveness=self._require_sender_liveness, lookup_only=self._lookup_only,
                            post_drain_only=self._post_drain)
                        try:
                            worker.serve_once()
                            if self._post_drain: self._outcome_sent.set()
                        except GateLedgerAcceptanceUnknown:
                            self._lookup_only = True
        except BaseException:
            failed = True
        finally:
            if failed:
                with self._lock: self._failed = True
                try: self._on_failure()
                except Exception: pass

    def post_drain_exchange(self):
        """If sealed, permit only one authenticated observation before shutdown."""
        if os.getpid() != self._pid: raise GateLedgerChannelError()
        with self._phase_lock:
            if not self.ledger.has_pair_drain_seal(): return
            if self._lookup_only or self._post_drain: raise GateLedgerChannelError()
            self._post_drain = True
        if not self._outcome_sent.wait(self._timeout): raise GateLedgerChannelError()

    def close(self):
        try:
            if os.getpid() != self._pid: raise ValueError()
            with self._lock:
                self._stopping = True
                thread = self._thread
            if thread is not None:
                if thread is threading.current_thread(): raise ValueError()
                thread.join()  # accepted effects must finish before ledger closure
            self._listener.close()
            if self._failed: raise ValueError()
        except Exception:
            raise GateLedgerChannelError() from None
