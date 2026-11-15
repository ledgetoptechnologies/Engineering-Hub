"""Pinned verification of fresh host app-cgroup observations; no effect authority."""
import base64
from dataclasses import dataclass
import math
import os
import re
import secrets
import time
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from engineering_hub.current_executor_observation import CurrentExecutorReleasePins, CurrentLaunchPins, _strict, _canonical
from .launch_transport import UnixCurrentObservationTransport

_DOMAIN = b'engineering-hub-app-cgroup-observation-v1\x00'
_FIELDS = {'protocol','nonce','launch_id','boot_id','engine_id','engine_pid','engine_start_ticks',
           'container_id','host_pid','host_start_ticks','cgroup_parent','cgroup_device','cgroup_inode',
           'namespace_device','namespace_inode','observed_at_ms','expires_at_ms'}


@dataclass(frozen=True)
class AppCgroupPins:
    parent: str
    device: int
    inode: int
    namespace_device: int
    namespace_inode: int

    def __post_init__(self):
        if (type(self.parent) is not str or not re.fullmatch(r'[a-z][a-z0-9-]{0,60}\.slice', self.parent)
                or any(type(v) is not int or not 0 < v < 2**63 for v in
                       (self.device, self.inode, self.namespace_device, self.namespace_inode))):
            raise ValueError('invalid independently enrolled app cgroup')


@dataclass(frozen=True)
class AppCgroupObservation:
    container_id: str
    expires_wall_ms: int
    expires_monotonic: float


class AppCgroupObserver:
    """Fresh signed evidence only; never authorizes an effect or a recovery."""
    def __init__(self, *, transport, release, launch, pins, timeout_ms):
        if (type(transport) is not UnixCurrentObservationTransport or type(release) is not CurrentExecutorReleasePins
                or type(launch) is not CurrentLaunchPins or type(pins) is not AppCgroupPins
                or type(timeout_ms) is not int or not 1 <= timeout_ms <= 10000):
            raise ValueError('explicit app cgroup observer required')
        self._transport, self._release, self._launch, self._pins = transport, release, launch, pins
        self._timeout_ms, self._pid, self._closed = timeout_ms, os.getpid(), False

    def verify(self, container_id, *, deadline):
        try:
            if (self._pid != os.getpid() or self._closed or type(container_id) is not str
                    or not re.fullmatch(r'[0-9a-f]{64}', container_id)
                    or type(deadline) not in (int,float) or not math.isfinite(deadline)):
                raise ValueError()
            started = time.monotonic()
            remaining = min(self._timeout_ms, int((deadline - started) * 1000))
            if remaining < 1: raise ValueError()
            nonce = secrets.token_hex(32)
            raw = self._transport.observe_app_cgroup(nonce=nonce, container_id=container_id, timeout_ms=remaining)
            envelope = _strict(raw)
            if type(envelope) is not dict or set(envelope) != {'payload','signature'}: raise ValueError()
            payload, signature = envelope['payload'], envelope['signature']
            if (type(payload) is not dict or set(payload) != _FIELDS or type(signature) is not str
                    or not re.fullmatch(r'[A-Za-z0-9_-]{86}', signature)): raise ValueError()
            signed = base64.urlsafe_b64decode(signature + '==')
            if len(signed) != 64 or base64.urlsafe_b64encode(signed).rstrip(b'=').decode() != signature: raise ValueError()
            Ed25519PublicKey.from_public_bytes(self._release.public_key).verify(signed, _DOMAIN + _canonical(payload))
            launch, pins = self._launch, self._pins
            expected = {'protocol':'engineering-hub.app-cgroup-observation.v1', 'nonce':nonce,
                'launch_id':launch.launch_id, 'boot_id':launch.boot_id, 'engine_id':launch.engine_id,
                'engine_pid':launch.engine_pid, 'engine_start_ticks':launch.engine_start_ticks,
                'container_id':container_id, 'cgroup_parent':pins.parent, 'cgroup_device':pins.device,
                'cgroup_inode':pins.inode, 'namespace_device':pins.namespace_device, 'namespace_inode':pins.namespace_inode}
            if any(type(payload[k]) is not type(v) or payload[k] != v for k,v in expected.items()): raise ValueError()
            if any(type(payload[k]) is not int or not 0 < payload[k] < 2**63 for k in
                   ('host_pid','host_start_ticks','observed_at_ms','expires_at_ms')): raise ValueError()
            now = int(time.time() * 1000)
            finished = time.monotonic()
            if (not payload['observed_at_ms'] <= now < payload['expires_at_ms'] <= launch.approval_expires_at_ms
                    or payload['expires_at_ms'] - payload['observed_at_ms'] > 5000
                    or finished >= deadline): raise ValueError()
            expiry = min(deadline, started + remaining / 1000,
                         finished + (payload['expires_at_ms'] - now) / 1000)
            if finished >= expiry: raise ValueError()
            return AppCgroupObservation(container_id, payload['expires_at_ms'], expiry)
        except Exception:
            raise ValueError('app cgroup observation unavailable') from None

    def close(self):
        self._closed = True
