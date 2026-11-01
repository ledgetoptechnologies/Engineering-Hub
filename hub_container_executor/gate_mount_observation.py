"""Nonce-bound supervisor observations for an explicitly enrolled gate mount.

The supervisor key and original launch receipt are independent trust inputs.
An observation is not a lease, exclusive-writer proof, or permission to start an
application. The caller must hold on uncertainty and retain host custody.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from .broker_seqpacket import _parse
from .build_record import VerifiedImage
from .runtime_identity import ExecutorReleaseEnrollment, LaunchReceiptIdentityVerifier

REQUEST = 'engineering-hub.executor-gate-mount-observation-request.v1'
PROTOCOL = 'engineering-hub.executor-gate-mount-observation.v1'
DOMAIN = b'engineering-hub-executor-gate-mount-observation-v1\x00'
FIELDS = frozenset({'protocol', 'nonce', 'launch_id', 'boot_id', 'authenticated_host_pid',
    'process_start_ticks', 'container_id', 'manifest_digest', 'launch_profile_digest',
    'socket_source_directory', 'policy_digest', 'directory_device', 'directory_inode',
    'socket_device', 'socket_inode', 'role', 'runnable_digest', 'platform_manifest_digest',
    'image_config_id', 'source_commit', 'bootstrap_digest', 'issued_at_ms', 'expires_at_ms'})
_SHA = re.compile(r'sha256:[0-9a-f]{64}\Z')


class GateMountObservationError(RuntimeError):
    def __init__(self):
        super().__init__('gate mount observation unavailable')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


class GateMountVerifier:
    """Own a read-only O_PATH pin and verify a new signed observation per check."""
    def __init__(self, *, release, launch_verifier, transport, manifest_digest, source_directory):
        self._fd = None
        try:
            if (type(release) is not ExecutorReleaseEnrollment
                    or type(launch_verifier) is not LaunchReceiptIdentityVerifier
                    or type(manifest_digest) is not str or not _SHA.fullmatch(manifest_digest)
                    or type(source_directory) is not str or not source_directory.startswith('/')
                    or source_directory == '/' or os.path.normpath(source_directory) != source_directory
                    or not hasattr(os, 'O_PATH') or not callable(getattr(transport, 'observe_gate_mount', None))):
                raise ValueError()
            self._pid, self._lock, self._closed = os.getpid(), threading.Lock(), False
            self._release, self._launch, self._transport = release, launch_verifier, transport
            self._manifest, self._source = manifest_digest, source_directory
            self._fd = os.open(source_directory, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            self._directory_identity()
        except Exception:
            if self._fd is not None: os.close(self._fd)
            self._fd = None
            raise GateMountObservationError() from None

    @property
    def source_directory(self):
        if os.getpid() != self._pid or self._closed: raise GateMountObservationError()
        return self._source

    def _directory_identity(self):
        current, named = os.fstat(self._fd), os.lstat(self._source)
        if (not stat.S_ISDIR(current.st_mode) or not stat.S_ISDIR(named.st_mode)
                or stat.S_IMODE(current.st_mode) != 0o711
                or Path(self._source).resolve(strict=True) != Path(self._source)
                or (current.st_dev, current.st_ino) != (named.st_dev, named.st_ino)
                or not os.statvfs(self._source).f_flag & os.ST_RDONLY):
            raise ValueError()
        return current.st_dev, current.st_ino

    def _socket_identity(self):
        info = os.stat('controller.sock', dir_fd=self._fd, follow_symlinks=False)
        if (not stat.S_ISSOCK(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o660
                or info.st_gid != os.getgid() or info.st_uid == os.getuid()):
            raise ValueError()
        return info.st_dev, info.st_ino

    def verify_current(self, image, *, timeout_ms):
        if os.getpid() != self._pid or not self._lock.acquire(blocking=False):
            raise GateMountObservationError()
        try:
            if (self._closed or type(image) is not VerifiedImage or type(timeout_ms) is not int
                    or not 1 <= timeout_ms <= 5000):
                raise ValueError()
            launch = self._launch.launch_binding()
            directory, sock = self._directory_identity(), self._socket_identity()
            start, wall_start = time.monotonic(), time.time() * 1000
            nonce = secrets.token_hex(32)
            raw = self._transport.observe_gate_mount(nonce=nonce, timeout_ms=timeout_ms)
            after, wall_after = time.monotonic(), time.time() * 1000
            if (type(raw) is not bytes or not 0 < len(raw) <= 8192
                    or not all(math.isfinite(v) for v in (start, after, wall_start, wall_after))
                    or after < start or after - start >= timeout_ms / 1000 or wall_after < wall_start):
                raise ValueError()
            envelope = _parse(raw)
            if set(envelope) != {'payload', 'signature'} or type(envelope['payload']) is not dict:
                raise ValueError()
            payload, signature = envelope['payload'], envelope['signature']
            if set(payload) != FIELDS or type(signature) is not str or not re.fullmatch(r'[0-9a-f]{128}', signature):
                raise ValueError()
            Ed25519PublicKey.from_public_bytes(self._release.public_key).verify(bytes.fromhex(signature),
                DOMAIN + _canonical(payload))
            expected = {'protocol': PROTOCOL, 'nonce': nonce, 'manifest_digest': self._manifest,
                'launch_profile_digest': self._release.launch_profile_digest, 'socket_source_directory': self._source,
                'role': image.role, 'runnable_digest': image.runnable_reference,
                'platform_manifest_digest': image.platform_manifest_digest, 'image_config_id': image.image_config_id,
                'source_commit': image.source_commit}
            if any(payload[name] != value for name, value in expected.items()):
                raise ValueError()
            launch_fields = ('launch_id', 'boot_id', 'authenticated_host_pid', 'process_start_ticks', 'container_id')
            if (len(launch) != len(launch_fields)
                    or any(type(payload[name]) is not type(value) or payload[name] != value
                           for name, value in zip(launch_fields, launch))):
                raise ValueError()
            if (type(payload['policy_digest']) is not str or not _SHA.fullmatch(payload['policy_digest'])
                    or type(payload['bootstrap_digest']) is not str or not _SHA.fullmatch(payload['bootstrap_digest'])
                    or any(type(payload[name]) is not int or payload[name] < 0 for name in
                           ('directory_device', 'directory_inode', 'socket_device', 'socket_inode'))
                    or (payload['directory_device'], payload['directory_inode']) != directory
                    or (payload['socket_device'], payload['socket_inode']) != sock):
                raise ValueError()
            issued, expires = payload['issued_at_ms'], payload['expires_at_ms']
            if (type(issued) is not int or type(expires) is not int or not 0 < expires - issued <= 5000
                    or not wall_start - 5000 <= issued <= wall_after < expires
                    or (after - start) * 1000 >= expires - wall_start
                    or self._directory_identity() != directory or self._socket_identity() != sock
                    or self._launch.launch_binding() != launch):
                raise ValueError()
            final, wall_final = time.monotonic(), time.time() * 1000
            if (not math.isfinite(final) or not math.isfinite(wall_final)
                    or final < after or wall_final < wall_after or wall_final >= expires
                    or final - start >= timeout_ms / 1000
                    or (final - start) * 1000 >= expires - wall_start):
                raise ValueError()
            return payload['bootstrap_digest']
        except Exception:
            raise GateMountObservationError() from None
        finally:
            self._lock.release()

    def close(self):
        if os.getpid() != self._pid: raise GateMountObservationError()
        with self._lock:
            if not self._closed:
                self._closed = True
                os.close(self._fd); self._fd = None
