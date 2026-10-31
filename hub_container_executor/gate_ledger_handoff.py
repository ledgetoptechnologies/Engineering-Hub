"""Verify one independently enrolled supervisor ledger listener handoff.

This verifies the signed listener FD, not exclusive custody, daemon isolation,
or the provenance of a path.  The host must independently enforce direct,
non-relayed channel custody; a verified receipt does not do so.
"""
import base64
import math
import os
import re
import socket
import stat
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from .runtime_identity import LaunchReceiptIdentityVerifier, _canonical, _strict

PROTOCOL = 'engineering-hub.gate-ledger-handoff.v1'
AUDIENCE = 'engineering-hub.container-executor-ledger'
DOMAIN = b'engineering-hub-gate-ledger-handoff-v1\x00'
FIELDS = frozenset({'protocol', 'audience', 'nonce', 'launch_id', 'boot_id',
    'authenticated_host_pid', 'process_start_ticks', 'container_id', 'manifest_digest',
    'launch_profile_digest', 'listener_device', 'listener_inode', 'path_device', 'path_inode',
    'issued_at_ms', 'expires_at_ms'})


class GateLedgerHandoffError(RuntimeError):
    def __init__(self): super().__init__('gate ledger handoff unavailable')


def verify_handoff(raw, fd, *, verifier, nonce, manifest_digest, source_directory, start_wall_ms, start_monotonic):
    """Reverify signed bytes and the exact borrowed listener on every use."""
    try:
        if (type(verifier) is not LaunchReceiptIdentityVerifier or type(raw) is not bytes
                or not 0 < len(raw) <= 8192 or type(fd) is not int or fd < 0
                or type(nonce) is not str or not re.fullmatch(r'[0-9a-f]{64}', nonce)
                or type(manifest_digest) is not str or not re.fullmatch(r'sha256:[0-9a-f]{64}', manifest_digest)
                or type(source_directory) is not str or not source_directory.startswith('/')
                or os.path.normpath(source_directory) != source_directory or source_directory.startswith('//')
                or type(start_wall_ms) is not int or start_wall_ms <= 0
                or type(start_monotonic) not in (int, float) or not math.isfinite(start_monotonic)):
            raise ValueError()
        binding = verifier.launch_binding()
        profile = verifier.launch_profile_digest()
        key = verifier.issuer_public_key()
        envelope = _strict(raw)
        if type(envelope) is not dict or set(envelope) != {'payload', 'signature'}: raise ValueError()
        payload, sig = envelope['payload'], envelope['signature']
        if type(payload) is not dict or set(payload) != FIELDS or type(sig) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{86}', sig):
            raise ValueError()
        signature = base64.b64decode(sig + '==', altchars=b'-_', validate=True)
        if len(signature) != 64 or base64.urlsafe_b64encode(signature).rstrip(b'=').decode() != sig: raise ValueError()
        Ed25519PublicKey.from_public_bytes(key).verify(signature, DOMAIN + _canonical(payload))
        observed = tuple(payload[k] for k in ('launch_id', 'boot_id', 'authenticated_host_pid',
                                              'process_start_ticks', 'container_id'))
        if (any(type(a) is not type(b) or a != b for a, b in zip(observed, binding))
                or payload['protocol'] != PROTOCOL or payload['audience'] != AUDIENCE
                or payload['nonce'] != nonce or payload['manifest_digest'] != manifest_digest
                or payload['launch_profile_digest'] != profile): raise ValueError()
        if any(type(payload[k]) is not int or payload[k] < (1 if k.endswith('inode') else 0)
               for k in ('listener_device', 'listener_inode', 'path_device', 'path_inode')): raise ValueError()
        issued, expires = payload['issued_at_ms'], payload['expires_at_ms']
        wall, tick = int(time.time() * 1000), time.monotonic()
        if (type(issued) is not int or type(expires) is not int or not 0 < expires - issued <= 5000
                or issued < start_wall_ms - 5000 or not issued <= wall < expires or wall < start_wall_ms
                or not start_monotonic <= tick or (tick - start_monotonic) * 1000 >= expires - start_wall_ms):
            raise ValueError()
        info = os.fstat(fd)
        if not stat.S_ISSOCK(info.st_mode) or (info.st_dev, info.st_ino) != (payload['listener_device'], payload['listener_inode']):
            raise ValueError()
        def check_path():
            leaf = os.lstat(os.path.join(source_directory, 'ledger.sock'))
            if not stat.S_ISSOCK(leaf.st_mode) or (leaf.st_dev, leaf.st_ino) != (payload['path_device'], payload['path_inode']):
                raise ValueError()
        check_path()
        borrowed = socket.socket(fileno=fd)
        try:
            if (borrowed.family != socket.AF_UNIX or borrowed.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET
                    or borrowed.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1): raise ValueError()
        finally:
            borrowed.detach()
        if verifier.launch_binding() != binding or verifier.launch_profile_digest() != profile: raise ValueError()
        check_path()
        if verifier.launch_binding() != binding or verifier.launch_profile_digest() != profile: raise ValueError()
        final_wall, final_tick = time.time() * 1000, time.monotonic()
        if (not math.isfinite(final_wall) or not math.isfinite(final_tick)
                or final_wall < wall or final_tick < tick or final_wall >= expires
                or (final_tick - start_monotonic) * 1000 >= expires - start_wall_ms): raise ValueError()
        return True
    except Exception:
        raise GateLedgerHandoffError() from None
