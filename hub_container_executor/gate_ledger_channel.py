"""Opt-in one-shot supervisor-to-owned-ledger transaction; not runtime wiring.

The caller must independently bind the public key to the approved supervisor,
authenticate the executor endpoint to the supervisor, and own/drain this call
before closing the ledger. A signed request does not prove host mount custody.
"""
import inspect
import hmac
import json
import math
import os
import re
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


class GateLedgerChannelError(RuntimeError):
    def __init__(self):
        super().__init__('gate ledger channel unavailable')


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
    authenticates requests in the opposite direction only.
    """
    def __init__(self, *, channel, ledger, supervisor_public_key, launch_verifier,
                 manifest_digest, hold_uncertain, timeout_seconds, completion_policy=None):
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
                    or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 5):
                raise ValueError()
            channel.getpeername()
            self._channel, self._ledger = channel, ledger
            self._key = Ed25519PublicKey.from_public_bytes(supervisor_public_key)
            self._launch, self._manifest, self._hold = launch_verifier, manifest_digest, hold_uncertain
            self._completion_policy = completion_policy
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
        def scope():
            now = time.monotonic()
            if os.getpid() != self._pid or not started <= now < deadline: raise ValueError()
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
                    or payload['manifest_digest'] != self._manifest or payload['action'] not in ('reserve', 'consume', 'inspect_completion', 'inspect_completion_v4')
                    or type(issued) is not int or type(expires) is not int or not 0 < expires - issued <= 5000
                    or now < wall or issued < wall - 5000 or not issued <= now < expires
                    or expires <= wall or (time.monotonic() - started) * 1000 >= expires - wall): raise ValueError()
            value = payload['value']
            if type(value) is not dict: raise ValueError()
            is_snapshot = payload['action'] in ('inspect_completion', 'inspect_completion_v4')
            identity = value.get('identity') if is_snapshot else value
            if type(identity) is not dict or identity.get('manifest_digest') != self._manifest: raise ValueError()
            last_wall = [now]
            def fresh_decision():
                current = int(time.time() * 1000)
                decision_issued, decision_expires = value['issued_at_ms'], value['expires_at_ms']
                if (current < last_wall[0] or decision_issued < wall - 5000
                        or not decision_issued <= current < decision_expires or decision_expires <= wall
                        or (time.monotonic() - started) * 1000 >= decision_expires - wall):
                    raise ValueError()
                last_wall[0] = current
            if is_snapshot:
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
            scope(); attempted = not is_snapshot
            if payload['action'] == 'reserve':
                self._ledger.reserve_mount_gate_challenge(value)
            elif payload['action'] == 'consume' and self._ledger.consume_mount_gate_challenge(value) is not True:
                raise ValueError()
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
                                      if is_snapshot else {'outcome': 'committed'})})
            if len(response) > MAX_BYTES: raise ValueError()
            scope(); self._channel.settimeout(deadline - time.monotonic())
            if payload['action'] == 'consume': fresh_decision()
            if not last_wall[0] <= int(time.time() * 1000) < expires: raise ValueError()
            if self._channel.send(response) != len(response): raise ValueError()
            sent = True
            return 'sent_unconfirmed'
        except Exception:
            raise GateLedgerChannelError() from None
        finally:
            if attempted and not sent:
                try: self._hold(json.loads(_canonical(value)))
                except Exception: pass


class SupervisorLedgerClient:
    """One authenticated request to a separately pinned executor process.

    The caller independently provisions the key, live launch binding and peer
    mapping. A lost response is uncertain and must never be retried blindly.
    The channel and peer are borrowed; the owner closes them after this call.
    """
    def __init__(self, *, channel, peer, signing_key, launch_binding, manifest_digest, timeout_seconds):
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
                    or not 0 < timeout_seconds <= 5): raise ValueError()
            channel.getpeername(); peer.check()
            self._channel, self._peer, self._key = channel, peer, signing_key
            self._binding, self._manifest, self._timeout = launch_binding, manifest_digest, timeout_seconds
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
            if action not in ('reserve', 'consume', 'inspect_completion', 'inspect_completion_v4') or type(value) is not dict: raise ValueError()
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
            expected = {'protocol': RESPONSE, 'challenge': challenge['challenge'], 'action': action,
                        'outcome': 'observed' if is_snapshot else 'committed'}
            if is_snapshot:
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
            return response['snapshot'] if is_snapshot else True
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
                 manifest_digest, hold_uncertain, timeout_seconds, completion_profile_bytes=None):
        try:
            if (type(listener) is not socket.socket or listener.family != socket.AF_UNIX
                    or listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET
                    or listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1
                    or type(ledger) is not SQLiteLedger or type(supervisor_public_key) is not bytes
                    or len(supervisor_public_key) != 32 or type(launch_verifier) is not LaunchReceiptIdentityVerifier
                    or not _sync(hold_uncertain)
                    or type(manifest_digest) is not str or not re.fullmatch(r'sha256:[0-9a-f]{64}', manifest_digest)
                    or ledger._expected[0] != manifest_digest or type(timeout_seconds) not in (int, float)
                    or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 5): raise ValueError()
            self.ledger = ledger
            if not hmac.compare_digest(launch_verifier.issuer_public_key(), supervisor_public_key):
                raise ValueError()
            completion_policy = None
            if completion_profile_bytes is not None:
                profile = verify_profile_bytes(completion_profile_bytes, launch_verifier.launch_profile_digest())
                if profile.gate_completion is None: raise ValueError()
                completion_policy = profile.gate_completion
            self._listener, self._key, self._launch = listener, supervisor_public_key, launch_verifier
            self._manifest, self._hold, self._timeout, self._on_failure = manifest_digest, hold_uncertain, timeout_seconds, None
            self._completion_policy = completion_policy
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
                    worker = GateLedgerWorker(channel=channel, ledger=self.ledger, supervisor_public_key=self._key,
                        launch_verifier=self._launch, manifest_digest=self._manifest, hold_uncertain=self._hold,
                        timeout_seconds=self._timeout, completion_policy=self._completion_policy)
                    worker.serve_once()
        except BaseException:
            failed = True
        finally:
            if failed:
                with self._lock: self._failed = True
                try: self._on_failure()
                except Exception: pass

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
