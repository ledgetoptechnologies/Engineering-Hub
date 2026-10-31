"""Root-owned paired handoff; no launcher or runtime gate is enabled here.

This explicit one-shot operation requires two already measured supervisors and
an independently enrolled pair signing key. It creates the socketpair itself.
Its private journal is never reused after an uncertain outcome. A committed
pair does not mean that either final confirmation was delivered.
"""
from __future__ import annotations

import array
import base64
import hashlib
import os
import socket
import stat
import struct
import time
from contextlib import ExitStack
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import handoff_identity as h
from .launch_supervisor import (LaunchSupervisor, LaunchSupervisorError, _Journal,
                                _canonical, _remaining, _require, _strict, _root_file, _path)
from .runtime_identity import MAX_RECEIPT_BYTES
from .launch_transport import MAX_REQUEST_BYTES


def _envelope(signer, domain, payload):
    signature = signer.sign(domain + _canonical(payload))
    return {'payload': payload, 'signature': base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii')}


def _transition(journal, pair_id, old_state, new_state, data):
    with journal.transaction() as db:
        _require(db.execute('UPDATE launch SET state=?,data=? WHERE id=? AND state=?',
                            (new_state, _canonical(data).decode(), pair_id, old_state)).rowcount == 1)


def _release_digest(release):
    data = {name: getattr(release, name) for name in release.__dataclass_fields__}
    data['public_key'] = data['public_key'].hex()
    data['platform'] = data['platform'].model_dump(mode='json')
    return 'sha256:' + hashlib.sha256(_canonical(data)).hexdigest()


def serve_pair(executor, coordinator, *, pair_id, enrollment_file, signing_key_file, journal_file,
               timeout_seconds):
    """Issue a paired channel once; uncertainty is retained, never retried.

    Both supervisors and their listeners are caller-owned and remain disabled by
    their normal constructors until separately accepted host integration exists.
    A fresh root-private journal is owned by this call and retained on disk.
    Pair confirmation delivery is not atomic and is not asserted by a commit.
    """
    _require(type(executor) is LaunchSupervisor and type(coordinator) is LaunchSupervisor
             and executor is not coordinator
             and type(pair_id) is str and str(UUID(pair_id)) == pair_id
             and type(timeout_seconds) in (int, float) and 0 < timeout_seconds <= 10)
    pid = os.getpid()
    deadline = time.monotonic() + timeout_seconds
    supervisors = {'executor': executor, 'coordinator': coordinator}
    peers, channels, reserved = {}, {}, []
    journal = None
    commit_attempted = False
    completed = False
    try:
        enrollment_path, key_path, journal_path = map(_path, (enrollment_file, signing_key_file, journal_file))
        _require(len({enrollment_path, key_path, journal_path}) == 3)
        raw_policy, policy_identity = _root_file(enrollment_path, 65536)
        raw_key, key_identity = _root_file(key_path, 32)
        _require(len(raw_key) == 32)
        policy = _strict(raw_policy, 65536)
        _require(type(policy) is dict and set(policy) == {'schema', 'pair_id', 'pair_public_key',
                 'manifest_digest', 'executor_launch_id', 'coordinator_launch_id',
                 'executor_release_digest', 'coordinator_release_digest'}
                 and policy['schema'] == 'engineering-hub.pair-handoff-enrollment.v1'
                 and policy['pair_id'] == pair_id)
        pair_signer = Ed25519PrivateKey.from_private_bytes(raw_key)
        _require(pair_signer.public_key().public_bytes(serialization.Encoding.Raw,
                 serialization.PublicFormat.Raw).hex() == policy['pair_public_key'])
        def pins_current():
            _require(_root_file(enrollment_path, 65536)[1] == policy_identity
                     and _root_file(key_path, 32)[1] == key_identity)
        with ExitStack() as stack:
            # Fixed ordering makes the two-supervisor lock acquisition stable.
            for role in ('executor', 'coordinator'):
                supervisor = supervisors[role]
                _require(supervisor._pid == pid)
                _require(supervisor._mutex.acquire(timeout=_remaining(deadline)))
                stack.callback(supervisor._mutex.release)
                supervisor._guard()
                _require((supervisor.profile.role or 'executor') == role
                         and supervisor.profile.command_channel == 'inherited-seqpacket-v1'
                         and supervisor._issuance_open and time.monotonic() < supervisor._expires)
                supervisor._validate_live(deadline)
            def hold_before_unlock():
                if not completed:
                    for role in reserved:
                        try:
                            supervisors[role]._hold()
                        except BaseException:
                            pass
            # Hold attempted launches before either supervisor mutex is released.
            stack.callback(hold_before_unlock)
            e, c = executor._consumer, coordinator._consumer
            _require(e.boot == c.boot and e.pid != c.pid and executor._container_id != coordinator._container_id
                     and executor._consumer_uid != coordinator._consumer_uid
                     and executor._consumer_gid != coordinator._consumer_gid
                     and executor._launch_id != coordinator._launch_id)
            manifests = {role: supervisor._handoff_manifest_digest() for role, supervisor in supervisors.items()}
            _require(manifests['executor'] == manifests['coordinator'] == policy['manifest_digest']
                     and policy['executor_launch_id'] == executor._launch_id
                     and policy['coordinator_launch_id'] == coordinator._launch_id
                     and policy['executor_release_digest'] == _release_digest(executor.release)
                     and policy['coordinator_release_digest'] == _release_digest(coordinator.release))
            pins_current()
            manifest = manifests['executor']
            left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            channels = {'executor': left, 'coordinator': right}
            journal = _Journal(str(journal_path), create=True)
            measurements = {'executor_launch_id': executor._launch_id,
                                      'coordinator_launch_id': coordinator._launch_id,
                                      'manifest_digest': manifest, 'boot_id': e.boot,
                                      'executor_pid': e.pid, 'executor_start': e.start,
                                      'coordinator_pid': c.pid, 'coordinator_start': c.start,
                                      'executor_container': executor._container_id,
                                      'coordinator_container': coordinator._container_id,
                                      'executor_uid': executor._consumer_uid, 'executor_gid': executor._consumer_gid,
                                      'coordinator_uid': coordinator._consumer_uid, 'coordinator_gid': coordinator._consumer_gid}
            journal.reserve(pair_id, measurements)
            requests, receipts = {}, {}
            for role in ('executor', 'coordinator'):
                supervisor = supervisors[role]
                supervisor._listener.settimeout(_remaining(deadline))
                peer, _ = supervisor._listener.accept()
                peers[role] = peer
                creds = struct.unpack('=iII', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                _require(creds == (supervisor._consumer.pid, supervisor._consumer_uid, supervisor._consumer_gid))
                supervisor._validate_live(deadline)
                size = struct.unpack('!I', supervisor._receive(peer, 4, deadline))[0]
                _require(0 < size <= MAX_REQUEST_BYTES)
                request = _strict(supervisor._receive(peer, size, deadline), MAX_REQUEST_BYTES)
                _require(type(request) is dict and set(request) == {'protocol', 'nonce'}
                         and request['protocol'] == (h.PAIR_EXECUTOR_REQUEST_PROTOCOL if role == 'executor'
                                                     else h.PAIR_COORDINATOR_REQUEST_PROTOCOL)
                         and type(request['nonce']) is str and h._HEX.fullmatch(request['nonce']))
                peer.settimeout(_remaining(deadline))
                _require(peer.recv(1) == b'')
                requests[role] = request
            _require(requests['executor']['nonce'] != requests['coordinator']['nonce'])
            for role in ('executor', 'coordinator'):
                supervisor = supervisors[role]
                supervisor._validate_live(deadline)
                supervisor._issuance_open = False
                reserved.append(role)
                # A failed commit/fsync may already have reserved this nonce.
                # Close issuance before attempting it; never retry uncertainty.
                supervisor._journal.reserve_issuance(supervisor._launch_id, requests[role]['nonce'])
                info = os.fstat(channels[role].fileno())
                _require(stat.S_ISSOCK(info.st_mode))
                now = int(time.time() * 1000)
                expires = min(now + int((supervisor._expires - time.monotonic()) * 1000),
                              supervisor._approval_expires_at_ms)
                _require(expires > now)
                release = supervisor.release
                payload = {'protocol': h.PAIR_EXECUTOR_PROTOCOL if role == 'executor' else h.PAIR_COORDINATOR_PROTOCOL,
                    'audience': h.AUDIENCE if role == 'executor' else h.COORDINATOR_AUDIENCE,
                    'nonce': requests[role]['nonce'], 'role': role, 'pair_id': pair_id,
                    'launch_id': supervisor._launch_id,
                    'peer_launch_id': supervisors['coordinator' if role == 'executor' else 'executor']._launch_id,
                    'boot_id': supervisor._consumer.boot, 'authenticated_host_pid': supervisor._consumer.pid,
                    'process_start_ticks': supervisor._consumer.start, 'container_id': supervisor._container_id,
                    'issuer_id': release.issuer_id, 'implementation_commit': release.implementation_commit,
                    'runnable_digest': release.runnable_digest, 'platform_manifest_digest': release.platform_manifest_digest,
                    'image_config_id': release.image_config_id, 'platform': release.platform.model_dump(mode='json'),
                    'launch_profile_digest': release.launch_profile_digest, 'manifest_digest': manifest,
                    'channel_device': info.st_dev, 'channel_inode': info.st_ino,
                    'issued_at_ms': now, 'expires_at_ms': expires}
                receipts[role] = _envelope(supervisor._signer,
                    h.PAIR_EXECUTOR_DOMAIN if role == 'executor' else h.PAIR_COORDINATOR_DOMAIN, payload)
            _transition(journal, pair_id, 'create_reserved', 'prepared',
                        {'measurements': measurements, 'executor_receipt': receipts['executor'],
                         'coordinator_receipt': receipts['coordinator']})
            for role in ('executor', 'coordinator'):
                supervisor, peer = supervisors[role], peers[role]
                supervisor._validate_live(deadline)
                raw = _canonical(receipts[role])
                _require(len(raw) <= MAX_RECEIPT_BYTES)
                packet = struct.pack('!I', len(raw)) + raw
                peer.settimeout(_remaining(deadline))
                _require(peer.sendmsg([packet], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                         array.array('i', [channels[role].fileno()]))]) == len(packet))
                supervisor._journal.issuance_state(supervisor._launch_id, 'sent', hashlib.sha256(raw).hexdigest())
            for role in ('executor', 'coordinator'):
                supervisors[role]._validate_live(deadline)
            pins_current()
            commit = {'protocol': h.PAIR_COMMIT_PROTOCOL, 'pair_id': pair_id, 'manifest_digest': manifest,
                      'executor_launch_id': executor._launch_id, 'coordinator_launch_id': coordinator._launch_id,
                      'executor_receipt': receipts['executor'], 'coordinator_receipt': receipts['coordinator'],
                      'executor_receipt_digest': 'sha256:' + hashlib.sha256(_canonical(receipts['executor'])).hexdigest(),
                      'coordinator_receipt_digest': 'sha256:' + hashlib.sha256(_canonical(receipts['coordinator'])).hexdigest()}
            encoded = _canonical(_envelope(pair_signer, h.PAIR_COMMIT_DOMAIN, commit))
            _require(len(encoded) <= MAX_RECEIPT_BYTES)
            # An error here can mean commit succeeded but fsync failed. Never
            # overwrite that uncertainty or send a confirmation on this path.
            commit_attempted = True
            _transition(journal, pair_id, 'prepared', 'committed',
                        {'measurements': measurements, 'commit_sha256': hashlib.sha256(encoded).hexdigest(),
                         'executor_receipt': receipts['executor'], 'coordinator_receipt': receipts['coordinator']})
            for role in ('executor', 'coordinator'):
                pins_current()
                supervisors[role]._validate_live(deadline)
                peers[role].settimeout(_remaining(deadline))
                peers[role].sendall(struct.pack('!I', len(encoded)) + encoded)
                peers[role].shutdown(socket.SHUT_WR)
                supervisors[role]._receipt_served = True
            completed = True
            return pair_id
    except BaseException:
        if journal is not None and not commit_attempted:
            try:
                with journal.transaction() as db:
                    db.execute("UPDATE launch SET state='held' WHERE id=? AND state IN ('create_reserved','prepared')", (pair_id,))
            except BaseException:
                pass
        raise LaunchSupervisorError() from None
    finally:
        for peer in peers.values():
            peer.close()
        for channel in channels.values():
            channel.close()
        if journal is not None:
            journal.close()
