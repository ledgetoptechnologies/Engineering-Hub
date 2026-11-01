"""One-shot authenticated local coordinator drain report over a private UDS.

This says only that the coordinator's local service cleanup returned. It does
not establish remote executor quiescence, resolve held effects, or grant reuse.
"""
import base64
import json
import os
import secrets
import socket
import struct
import time
from dataclasses import dataclass
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from .launch_transport import _read_exact, _remaining, _socket_leaf, _validate_transport_options

REQUEST = 'engineering-hub.coordinator-local-drain-request.v1'
RESPONSE = 'engineering-hub.coordinator-local-drain-receipt.v1'
AUDIENCE = 'engineering-hub.private-coordinator'
DOMAIN = b'engineering-hub-coordinator-local-drain-v1\x00'
MAX_BYTES = 4096


@dataclass(frozen=True)
class LocalCoordinatorDrain:
    """In-process evidence of local cleanup, not remote executor quiescence."""
    owner_epoch: str
    manifest_digest: str


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def report_local_drain(*, socket_path, issuer_uid, issuer_gid, release, binding, owner_epoch, config_digest, session_digest, timeout_ms):
    """Send once and verify the original launch-bound durable ACK; no retry."""
    from .runtime_identity import CoordinatorReleaseEnrollment
    try:
        _validate_transport_options(socket_path, issuer_uid, issuer_gid)
        if (type(release) is not CoordinatorReleaseEnrollment or type(binding) is not tuple or len(binding) != 5
                or type(owner_epoch) is not str or str(UUID(owner_epoch)) != owner_epoch
                or type(timeout_ms) is not int or not 100 <= timeout_ms <= 10000): raise ValueError()
        launch_id, boot_id, pid, start, container = binding
        request = {'protocol': REQUEST, 'nonce': secrets.token_hex(32), 'launch_id': launch_id,
            'boot_id': boot_id, 'authenticated_host_pid': pid, 'process_start_ticks': start,
            'container_id': container, 'owner_epoch': owner_epoch, 'launch_profile_digest': release.launch_profile_digest,
            'config_digest': config_digest, 'session_digest': session_digest}
        raw = canonical(request)
        if len(raw) > MAX_BYTES: raise ValueError()
        deadline = time.monotonic() + timeout_ms / 1000
        identity = _socket_leaf(socket_path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(_remaining(deadline)); connection.connect(socket_path)
            peer = struct.unpack('=iII', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if peer[1:] != (issuer_uid, issuer_gid) or _socket_leaf(socket_path) != identity: raise ValueError()
            connection.settimeout(_remaining(deadline)); connection.sendall(struct.pack('!I', len(raw)) + raw)
            connection.shutdown(socket.SHUT_WR)
            size = struct.unpack('!I', _read_exact(connection, 4, deadline))[0]
            if not 0 < size <= MAX_BYTES: raise ValueError()
            response = _read_exact(connection, size, deadline)
            connection.settimeout(_remaining(deadline))
            if connection.recv(1) != b'' or _socket_leaf(socket_path) != identity: raise ValueError()
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError()
                result[key] = value
            return result
        envelope = json.loads(response, object_pairs_hook=unique)
        if type(envelope) is not dict or set(envelope) != {'payload', 'signature'} or canonical(envelope) != response:
            raise ValueError()
        payload, signature = envelope['payload'], envelope['signature']
        expected = dict(request, protocol=RESPONSE, audience=AUDIENCE)
        if type(payload) is not dict or payload != expected or type(signature) is not str: raise ValueError()
        decoded = base64.b64decode(signature + '==', altchars=b'-_', validate=True)
        if len(decoded) != 64 or base64.urlsafe_b64encode(decoded).rstrip(b'=').decode() != signature: raise ValueError()
        Ed25519PublicKey.from_public_bytes(release.public_key).verify(decoded, DOMAIN + canonical(payload))
        _remaining(deadline)
        return True
    except Exception:
        raise RuntimeError('coordinator local drain unavailable') from None
