"""Private verifier for the gated executor channel handoff protocol.

The root supervisor must authenticate its exact consumer, validate the running
container and independently approved manifest before signing. A receipt is not
proof of isolation, coordinator launch or continuing integrity. This module
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
                           start_monotonic, start_wall_ms, max_lifetime_seconds):
    """Validate receipt and exact received FD; caller owns FD on all paths.

    The verifier authenticates the issuer's process mapping, not the mapping on
    its own. It does not authenticate the opposite endpoint, its owner or its
    custody; this receipt alone is not a paired-launch authorization. The
    independently protected enrollment and direct authenticated issuer transport
    remain mandatory; this function creates no such authority.
    """
    try:
        _require(type(enrollment) is ExecutorReleaseEnrollment)
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
        Ed25519PublicKey.from_public_bytes(enrolled.public_key).verify(decoded, SIGNATURE_DOMAIN + _canonical(payload))
        _require(payload['protocol'] == PROTOCOL and payload['audience'] == AUDIENCE
                 and payload['nonce'] == nonce and payload['role'] == 'executor'
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


def verify_channel_commit(raw, receipt, *, enrollment, nonce, launch_id):
    """Verify signed confirmation of the exact durably committed receipt."""
    try:
        _require(type(enrollment) is ExecutorReleaseEnrollment and type(receipt) is bytes)
        envelope = _strict(raw)
        _require(type(envelope) is dict and set(envelope) == {'payload', 'signature'})
        payload, signature = envelope['payload'], envelope['signature']
        _require(type(payload) is dict and set(payload) == {'protocol', 'nonce', 'launch_id', 'receipt_digest'}
                 and payload['protocol'] == COMMIT_PROTOCOL and payload['nonce'] == nonce
                 and payload['launch_id'] == launch_id
                 and payload['receipt_digest'] == 'sha256:' + hashlib.sha256(receipt).hexdigest()
                 and type(signature) is str and _SIGNATURE.fullmatch(signature))
        decoded = base64.b64decode(signature + '==', altchars=b'-_', validate=True)
        _require(len(decoded) == 64 and base64.urlsafe_b64encode(decoded).rstrip(b'=').decode() == signature)
        Ed25519PublicKey.from_public_bytes(enrollment.public_key).verify(decoded, COMMIT_DOMAIN + _canonical(payload))
    except Exception:
        raise RuntimeIdentityError('Trusted channel handoff unavailable') from None
