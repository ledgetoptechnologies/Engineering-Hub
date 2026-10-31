"""Private verifier for the gated role-specific channel handoff protocol.

The root supervisor must authenticate its exact consumer, validate the running
container and independently approved manifest before signing. A receipt is not
proof of isolation, paired launch or continuing integrity. This module
never provisions, issues, discovers keys or starts a service.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import stat
import time
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from .runtime_identity import ExecutorReleaseEnrollment, RuntimeIdentityError, _strict, _canonical

PROTOCOL = 'engineering-hub.launch-channel.v1'
REQUEST_PROTOCOL = 'engineering-hub.launch-channel-request.v1'
AUDIENCE = 'engineering-hub.container-executor'
SIGNATURE_DOMAIN = b'engineering-hub-launch-channel-v1\x00'
COMMIT_PROTOCOL = 'engineering-hub.launch-channel-commit.v1'
COMMIT_DOMAIN = b'engineering-hub-launch-channel-commit-v1\x00'
COORDINATOR_PROTOCOL = 'engineering-hub.coordinator-channel.v1'
COORDINATOR_REQUEST_PROTOCOL = 'engineering-hub.coordinator-channel-request.v1'
COORDINATOR_AUDIENCE = 'engineering-hub.pilot-coordinator'
COORDINATOR_DOMAIN = b'engineering-hub-coordinator-channel-v1\x00'
COORDINATOR_COMMIT_PROTOCOL = 'engineering-hub.coordinator-channel-commit.v1'
COORDINATOR_COMMIT_DOMAIN = b'engineering-hub-coordinator-channel-commit-v1\x00'
_FIELDS = frozenset({'protocol','audience','nonce','role','launch_id','boot_id',
    'authenticated_host_pid','process_start_ticks','container_id','issuer_id',
    'implementation_commit','runnable_digest','platform_manifest_digest','image_config_id',
    'platform','launch_profile_digest','manifest_digest','channel_device','channel_inode',
    'issued_at_ms','expires_at_ms'})
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_SIGNATURE = re.compile(r'[A-Za-z0-9_-]{86}\Z')
_SHA = re.compile(r'sha256:[0-9a-f]{64}\Z')


def _require(value):
    if not value:
        raise RuntimeIdentityError('Trusted channel handoff unavailable')


def verify_channel_receipt(raw, fd, *, enrollment, nonce, manifest_digest,
                           start_monotonic, start_wall_ms, max_lifetime_seconds, role='executor'):
    """Validate receipt and exact received FD; caller owns FD on all paths.

    The verifier authenticates the issuer's process mapping, not the mapping on
    its own. It does not authenticate the opposite endpoint, its owner or its
    custody; this receipt alone is not a paired-launch authorization. The
    independently protected enrollment and direct authenticated issuer transport
    remain mandatory; this function creates no such authority.
    """
    try:
        _require(type(enrollment) is ExecutorReleaseEnrollment and role in {'executor', 'coordinator'})
        enrolled = ExecutorReleaseEnrollment(**{name: getattr(enrollment, name)
            for name in ExecutorReleaseEnrollment.__dataclass_fields__})
        _require(type(nonce) is str and _HEX.fullmatch(nonce)
                 and type(manifest_digest) is str and _SHA.fullmatch(manifest_digest)
                 and type(max_lifetime_seconds) is int and 1 <= max_lifetime_seconds <= 3600)
        envelope = _strict(raw)
        _require(type(envelope) is dict and set(envelope) == {'payload', 'signature'})
        payload, signature = envelope['payload'], envelope['signature']
        _require(type(payload) is dict and set(payload) == _FIELDS and type(signature) is str
                 and _SIGNATURE.fullmatch(signature))
        decoded = base64.b64decode(signature + '==', altchars=b'-_', validate=True)
        _require(len(decoded) == 64 and base64.urlsafe_b64encode(decoded).rstrip(b'=').decode() == signature)
        domain = SIGNATURE_DOMAIN if role == 'executor' else COORDINATOR_DOMAIN
        Ed25519PublicKey.from_public_bytes(enrolled.public_key).verify(decoded, domain + _canonical(payload))
        _require(payload['protocol'] == (PROTOCOL if role == 'executor' else COORDINATOR_PROTOCOL)
                 and payload['audience'] == (AUDIENCE if role == 'executor' else COORDINATOR_AUDIENCE)
                 and payload['nonce'] == nonce and payload['role'] == role
                 and payload['manifest_digest'] == manifest_digest)
        for name in ('launch_id','boot_id'):
            _require(type(payload[name]) is str and str(UUID(payload[name])) == payload[name])
        for name in ('authenticated_host_pid','process_start_ticks','issued_at_ms','expires_at_ms',
                     'channel_inode'):
            _require(type(payload[name]) is int and 0 < payload[name] <= 2**63 - 1)
        _require(type(payload['channel_device']) is int and 0 <= payload['channel_device'] <= 2**63 - 1
                 and type(payload['container_id']) is str and _HEX.fullmatch(payload['container_id']))
        for name in ('issuer_id','implementation_commit','runnable_digest','platform_manifest_digest',
                     'image_config_id','launch_profile_digest'):
            _require(type(payload[name]) is str and payload[name] == getattr(enrolled, name))
        _require(payload['platform'] == enrolled.platform.model_dump(mode='json'))
        now_wall = int(time.time() * 1000)
        now_mono = time.monotonic()
        issued, expires = payload['issued_at_ms'], payload['expires_at_ms']
        _require(type(start_wall_ms) is int and start_wall_ms - 5000 <= issued <= now_wall + 5000
                 and 0 < expires-issued <= max_lifetime_seconds * 1000 and now_wall < expires
                 and start_monotonic <= now_mono < start_monotonic + (expires - issued) / 1000
                 and now_mono < start_monotonic + (expires - start_wall_ms) / 1000)
        info = os.fstat(fd)
        _require(stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) ==
                 (payload['channel_device'], payload['channel_inode']))
        borrowed = socket.socket(fileno=fd)
        try:
            _require(borrowed.family == socket.AF_UNIX and borrowed.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) == socket.SOCK_SEQPACKET
                     and borrowed.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 0)
            borrowed.getpeername()
        finally:
            borrowed.detach()
        os.set_inheritable(fd, False)
        return payload['launch_id']
    except Exception:
        raise RuntimeIdentityError('Trusted channel handoff unavailable') from None


def verify_channel_commit(raw, receipt, *, enrollment, nonce, launch_id, role='executor'):
    """Verify signed confirmation of the exact durably committed receipt."""
    try:
        _require(type(enrollment) is ExecutorReleaseEnrollment and type(receipt) is bytes
                 and role in {'executor', 'coordinator'})
        envelope = _strict(raw)
        _require(type(envelope) is dict and set(envelope) == {'payload', 'signature'})
        payload, signature = envelope['payload'], envelope['signature']
        _require(type(payload) is dict and set(payload) == {'protocol', 'nonce', 'launch_id', 'receipt_digest'}
                 and payload['protocol'] == (COMMIT_PROTOCOL if role == 'executor' else COORDINATOR_COMMIT_PROTOCOL)
                 and payload['nonce'] == nonce
                 and payload['launch_id'] == launch_id
                 and payload['receipt_digest'] == 'sha256:' + hashlib.sha256(receipt).hexdigest()
                 and type(signature) is str and _SIGNATURE.fullmatch(signature))
        decoded = base64.b64decode(signature + '==', altchars=b'-_', validate=True)
        _require(len(decoded) == 64 and base64.urlsafe_b64encode(decoded).rstrip(b'=').decode() == signature)
        Ed25519PublicKey.from_public_bytes(enrollment.public_key).verify(decoded,
            (COMMIT_DOMAIN if role == 'executor' else COORDINATOR_COMMIT_DOMAIN) + _canonical(payload))
    except Exception:
        raise RuntimeIdentityError('Trusted channel handoff unavailable') from None

# Pair receipts use a distinct protocol and signing domains from single-role v1.
PAIR_EXECUTOR_PROTOCOL = 'engineering-hub.executor-pair-channel.v2'
PAIR_COORDINATOR_PROTOCOL = 'engineering-hub.coordinator-pair-channel.v2'
PAIR_EXECUTOR_REQUEST_PROTOCOL = 'engineering-hub.executor-pair-channel-request.v2'
PAIR_COORDINATOR_REQUEST_PROTOCOL = 'engineering-hub.coordinator-pair-channel-request.v2'
PAIR_EXECUTOR_DOMAIN = b'engineering-hub-executor-pair-channel-v2\x00'
PAIR_COORDINATOR_DOMAIN = b'engineering-hub-coordinator-pair-channel-v2\x00'
PAIR_COMMIT_PROTOCOL = 'engineering-hub.pair-channel-commit.v2'
PAIR_COMMIT_DOMAIN = b'engineering-hub-pair-channel-commit-v2\x00'
_PAIR_FIELDS = _FIELDS | {'pair_id', 'peer_launch_id'}
_PAIR_COMMIT_FIELDS = frozenset({'protocol', 'pair_id', 'manifest_digest', 'executor_launch_id',
    'coordinator_launch_id', 'executor_receipt', 'coordinator_receipt',
    'executor_receipt_digest', 'coordinator_receipt_digest'})


def _signature(envelope, public_key, domain):
    _require(type(envelope) is dict and set(envelope) == {'payload', 'signature'}
             and type(envelope['signature']) is str and _SIGNATURE.fullmatch(envelope['signature']))
    signature = base64.b64decode(envelope['signature'] + '==', altchars=b'-_', validate=True)
    _require(len(signature) == 64 and base64.urlsafe_b64encode(signature).rstrip(b'=').decode() == envelope['signature'])
    Ed25519PublicKey.from_public_bytes(public_key).verify(signature, domain + _canonical(envelope['payload']))
    return envelope['payload']


def _canonical_uuid(value):
    return type(value) is str and str(UUID(value)) == value


def _verify_pair_role(envelope, *, enrollment, role, pair_id, launch_id, peer_launch_id,
                      manifest_digest, now_wall, maximum_lifetime):
    _require(type(enrollment) is ExecutorReleaseEnrollment)
    enrolled = ExecutorReleaseEnrollment(**{name: getattr(enrollment, name)
        for name in ExecutorReleaseEnrollment.__dataclass_fields__})
    payload = _signature(envelope, enrolled.public_key,
                         PAIR_EXECUTOR_DOMAIN if role == 'executor' else PAIR_COORDINATOR_DOMAIN)
    _require(type(payload) is dict and set(payload) == _PAIR_FIELDS
             and payload['protocol'] == (PAIR_EXECUTOR_PROTOCOL if role == 'executor' else PAIR_COORDINATOR_PROTOCOL)
             and payload['audience'] == (AUDIENCE if role == 'executor' else COORDINATOR_AUDIENCE)
             and payload['role'] == role and payload['pair_id'] == pair_id
             and payload['launch_id'] == launch_id and payload['peer_launch_id'] == peer_launch_id
             and payload['manifest_digest'] == manifest_digest
             and type(payload['nonce']) is str and _HEX.fullmatch(payload['nonce'])
             and _canonical_uuid(payload['boot_id'])
             and type(payload['container_id']) is str and _HEX.fullmatch(payload['container_id']))
    for name in ('authenticated_host_pid', 'process_start_ticks', 'issued_at_ms', 'expires_at_ms', 'channel_inode'):
        _require(type(payload[name]) is int and 0 < payload[name] <= 2**63 - 1)
    _require(type(payload['channel_device']) is int and 0 <= payload['channel_device'] <= 2**63 - 1)
    for name in ('issuer_id', 'implementation_commit', 'runnable_digest', 'platform_manifest_digest',
                 'image_config_id', 'launch_profile_digest'):
        _require(type(payload[name]) is str and payload[name] == getattr(enrolled, name))
    _require(payload['platform'] == enrolled.platform.model_dump(mode='json')
             and payload['issued_at_ms'] <= now_wall + 5000 and now_wall < payload['expires_at_ms']
             and 0 < payload['expires_at_ms'] - payload['issued_at_ms'] <= maximum_lifetime * 1000)
    return payload


def verify_pair_channel_receipts(raw, commit_raw, fd, *, role, nonce, pair_id, manifest_digest,
                                 executor_launch_id, coordinator_launch_id, executor_enrollment,
                                 coordinator_enrollment, pair_authority_public_key, start_monotonic,
                                 start_wall_ms, max_lifetime_seconds):
    """Verify both role endorsements and pair commit before releasing an owned FD.

    All pins come from independent enrollment. This verifies signed statements,
    not kernel isolation, actual peer FD custody or approval of the inputs.
    """
    try:
        _require(role in {'executor', 'coordinator'} and type(nonce) is str and _HEX.fullmatch(nonce)
                 and all(_canonical_uuid(value) for value in (pair_id, executor_launch_id, coordinator_launch_id))
                 and executor_launch_id != coordinator_launch_id
                 and type(manifest_digest) is str and _SHA.fullmatch(manifest_digest)
                 and type(pair_authority_public_key) is bytes and len(pair_authority_public_key) == 32
                 and type(max_lifetime_seconds) is int and 1 <= max_lifetime_seconds <= 3600
                 and type(start_wall_ms) is int and type(start_monotonic) in (int, float))
        first = _strict(raw)
        commit_envelope = _strict(commit_raw)
        commit = _signature(commit_envelope, pair_authority_public_key, PAIR_COMMIT_DOMAIN)
        _require(type(commit) is dict and set(commit) == _PAIR_COMMIT_FIELDS
                 and commit['protocol'] == PAIR_COMMIT_PROTOCOL and commit['pair_id'] == pair_id
                 and commit['manifest_digest'] == manifest_digest
                 and commit['executor_launch_id'] == executor_launch_id
                 and commit['coordinator_launch_id'] == coordinator_launch_id)
        now_wall, now_mono = int(time.time() * 1000), time.monotonic()
        _require(now_wall >= start_wall_ms and now_mono >= start_monotonic)
        executor = _verify_pair_role(commit['executor_receipt'], enrollment=executor_enrollment,
            role='executor', pair_id=pair_id, launch_id=executor_launch_id, peer_launch_id=coordinator_launch_id,
            manifest_digest=manifest_digest, now_wall=now_wall, maximum_lifetime=max_lifetime_seconds)
        coordinator = _verify_pair_role(commit['coordinator_receipt'], enrollment=coordinator_enrollment,
            role='coordinator', pair_id=pair_id, launch_id=coordinator_launch_id, peer_launch_id=executor_launch_id,
            manifest_digest=manifest_digest, now_wall=now_wall, maximum_lifetime=max_lifetime_seconds)
        _require(all(now_mono < start_monotonic + (item['expires_at_ms'] - start_wall_ms) / 1000
                     for item in (executor, coordinator))
                 and executor['boot_id'] == coordinator['boot_id']
                 and executor['authenticated_host_pid'] != coordinator['authenticated_host_pid']
                 and executor['container_id'] != coordinator['container_id']
                 and (executor['channel_device'], executor['channel_inode']) !=
                     (coordinator['channel_device'], coordinator['channel_inode']))
        for name in ('executor', 'coordinator'):
            receipt_raw = _canonical(commit[name + '_receipt'])
            _require(commit[name + '_receipt_digest'] == 'sha256:' + hashlib.sha256(receipt_raw).hexdigest())
        own = executor if role == 'executor' else coordinator
        _require(first == commit[role + '_receipt'] and _canonical(first) == raw
                 and own['nonce'] == nonce and start_wall_ms - 5000 <= own['issued_at_ms']
                 and start_monotonic <= now_mono < start_monotonic + (own['expires_at_ms'] - own['issued_at_ms']) / 1000
                 and now_mono < start_monotonic + (own['expires_at_ms'] - start_wall_ms) / 1000)
        info = os.fstat(fd)
        _require(stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) ==
                 (own['channel_device'], own['channel_inode']))
        borrowed = socket.socket(fileno=fd)
        try:
            _require(borrowed.family == socket.AF_UNIX
                     and borrowed.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) == socket.SOCK_SEQPACKET
                     and borrowed.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 0)
            borrowed.getpeername()
        finally:
            borrowed.detach()
        os.set_inheritable(fd, False)
        return pair_id
    except Exception:
        raise RuntimeIdentityError('Trusted pair channel handoff unavailable') from None
