"""Synthetic issuer signatures, not installed host containment."""
import base64
from dataclasses import replace
import time
from uuid import uuid4
import pytest

pytest.importorskip('fcntl')
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from engineering_hub.container_pilot_manifest import Platform
from engineering_hub.current_executor_observation import CurrentExecutorReleasePins, CurrentLaunchPins, _canonical
from hub_container_executor.app_cgroup_observation import AppCgroupObserver, AppCgroupPins, _DOMAIN
from hub_container_executor.launch_transport import UnixCurrentObservationTransport


@pytest.mark.parametrize('mutation', ['none','nonce','container','engine','parent','scope','namespace','expired','signature'])
def test_signed_observation_requires_exact_live_pins(monkeypatch, mutation):
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    release = CurrentExecutorReleasePins('issuer', pub, 'c'*40, 'sha256:'+'1'*64,
        'sha256:'+'2'*64, 'sha256:'+'3'*64, Platform(os='linux',architecture='amd64',variant=None), 'sha256:'+'4'*64)
    now = int(time.time()*1000)
    launch = CurrentLaunchPins(str(uuid4()), str(uuid4()), 20, 30, 'a'*64, 'engine', 40, 50, now+30000)
    pins = AppCgroupPins('parent.slice', 11, 12, 13, 14)
    transport = UnixCurrentObservationTransport.__new__(UnixCurrentObservationTransport)
    def exchange(*, nonce, container_id, timeout_ms):
        payload = {'protocol':'engineering-hub.app-cgroup-observation.v1','nonce':nonce,
            'launch_id':launch.launch_id,'boot_id':launch.boot_id,'engine_id':launch.engine_id,
            'engine_pid':launch.engine_pid,'engine_start_ticks':launch.engine_start_ticks,
            'container_id':container_id,'host_pid':60,'host_start_ticks':70,'cgroup_parent':pins.parent,
            'cgroup_device':pins.device,'cgroup_inode':pins.inode,'namespace_device':pins.namespace_device,
            'namespace_inode':pins.namespace_inode,'observed_at_ms':now,'expires_at_ms':now+3000}
        changes = {'nonce':('nonce','f'*64),'container':('container_id','b'*64),
                   'engine':('engine_pid',41),'parent':('cgroup_parent','other.slice'),
                   'scope':('cgroup_inode',99),'namespace':('namespace_inode',99),
                   'expired':('expires_at_ms',now-1)}
        if mutation in changes:
            field,value = changes[mutation]; payload[field] = value
        sig = key.sign(_DOMAIN + _canonical(payload))
        if mutation == 'signature': sig = bytes(64)
        return _canonical({'payload':payload,'signature':base64.urlsafe_b64encode(sig).rstrip(b'=').decode()})
    monkeypatch.setattr(transport, 'observe_app_cgroup', exchange)
    observer = AppCgroupObserver(transport=transport, release=release, launch=launch, pins=pins, timeout_ms=1000)
    if mutation == 'none':
        assert observer.verify('a'*64, deadline=time.monotonic()+2).container_id == 'a'*64
    else:
        with pytest.raises(ValueError): observer.verify('a'*64, deadline=time.monotonic()+2)
