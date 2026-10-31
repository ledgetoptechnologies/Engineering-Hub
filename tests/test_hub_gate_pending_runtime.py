"""Synthetic daemon/proc mapping; real no-follow generation descriptors."""
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from hub_container_executor import launch_supervisor as launch
from hub_container_executor import application_gate_enrollment as enrollment
from tests.test_hub_gate_socket_supervisor import socket_rig
from tests.test_hub_container_executor_launch_supervisor import short_root


@pytest.mark.parametrize('fault', ['none', 'not_created', 'created', 'wrong_image', 'wrong_source'])
def test_pending_runtime_binds_daemon_and_generation(socket_rig, tmp_path, monkeypatch, fault):
    supervisor, manifest, image, _, _, held = socket_rig
    root = tmp_path / 'root'; generations = root / 'var/lib'; source = generations / 'hub'
    source.mkdir(parents=True)
    source_info, root_info = os.stat(source), os.stat(generations)
    enrolled = SimpleNamespace(schema=enrollment.COMPLETION_SCHEMA, role=image.role,
        image_config_id=image.image_config_id, runnable_digest=image.runnable_reference,
        platform_manifest_digest=image.platform_manifest_digest, source_commit=image.source_commit,
        bootstrap_digest='sha256:' + 'a' * 64, consumer_host_uid=os.getuid(), consumer_host_gid=os.getgid())
    supervisor._gate_enrollment = enrolled
    supervisor._consumer = SimpleNamespace(boot=str(uuid4()))
    supervisor._mounts['generations'] = SimpleNamespace(source=str(generations))
    supervisor._source_pins = {'generations': launch._identity(root_info)}
    context = {'operation_id': str(uuid4()), 'run_id': str(uuid4()), 'fence': 7, 'role': image.role,
        'generation_id': 'hub', 'container_id': 'a' * 64, 'image_config_id': image.image_config_id,
        'runnable_digest': image.runnable_reference, 'platform_manifest_digest': image.platform_manifest_digest}
    supervisor.pending_gate_context = lambda **_: None if fault == 'not_created' else context
    state = {'Running': fault != 'created', 'Status': 'created' if fault == 'created' else 'running',
             'Paused': False, 'Restarting': False, 'Dead': False, 'Pid': os.getpid()}
    supervisor._engine = SimpleNamespace(json=lambda *_: (200, {'Id': context['container_id'],
        'Image': 'sha256:' + '0' * 64 if fault == 'wrong_image' else image.image_config_id, 'State': state}))
    monkeypatch.setattr(enrollment, 'load_application_gate_enrollment', lambda *_, **__: enrolled)
    monkeypatch.setattr(supervisor, '_gate_socket_check', lambda _: None)
    observed = []
    monkeypatch.setattr(launch, '_measure_gate_process', lambda peer, pins, digest, deadline, **kw: observed.append((pins, digest, kw)))
    original_open = os.open
    def redirected(path, flags, *args, **kwargs):
        if str(path) == f'/proc/{os.getpid()}/root': path = str(tmp_path if fault == 'wrong_source' else root)
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(launch.os, 'open', redirected)
    if fault in ('wrong_image', 'wrong_source'):
        with pytest.raises(launch.LaunchSupervisorError):
            supervisor.observe_pending_gate_runtime(manifest=manifest, image=image, enrollment_file='synthetic', timeout_seconds=2)
        assert held
    else:
        pins = supervisor.observe_pending_gate_runtime(manifest=manifest, image=image, enrollment_file='synthetic', timeout_seconds=2)
        if fault in ('not_created', 'created'):
            assert pins is None and not observed
        else:
            assert (pins['expected_device'], pins['expected_inode']) == (source_info.st_dev, source_info.st_ino)
            assert pins['operation_id'] == context['operation_id'] and pins['fence'] == context['fence']
            assert len(observed) == 1 and observed[0][2] == {'bootstrap': True}
        assert not held
