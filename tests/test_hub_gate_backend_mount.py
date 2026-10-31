"""Closed Docker transcript; synthetic image and observation authority."""
import os
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from hub_container_executor.application_gate_enrollment import STATIC_ENTRYPOINT
from hub_container_executor.backend import ContainerPlan, ImageDefaults
from hub_container_executor.daemon_backend import DaemonBackendError, DockerEngineBackend
from hub_container_executor.gate_mount_observation import GateMountVerifier
from tests.test_hub_application_gate_enrollment import vector
from tests.test_hub_container_executor_daemon_backend import binding, container_inspect, make_profile, safe_generation
from tests.test_hub_gate_bootstrap_archive import archive, digest, elf


@pytest.fixture
def rig(tmp_path, monkeypatch):
    manifest, image, _supervisor, _policy = vector()
    image = replace(image, image_config={'config': {'Entrypoint': list(STATIC_ENTRYPOINT), 'Cmd': [], 'Env': []}})
    verifier = object.__new__(GateMountVerifier)
    verifier._pid, verifier._closed, verifier._source = os.getpid(), False, '/operator/gate'
    calls, control = [], {'digest': digest(elf()), 'fail_observation': False, 'fail_at': None, 'bad_archive': False}
    def observe(_self, selected, *, timeout_ms):
        calls.append('observe')
        if control['fail_observation'] or control['fail_at'] == len(calls): raise RuntimeError('synthetic private error')
        assert selected == image and 1 <= timeout_ms <= 5000
        return control['digest']
    monkeypatch.setattr(GateMountVerifier, 'verify_current', observe)
    source = safe_generation(tmp_path)
    profile = make_profile(image)
    profile = replace(profile, mounts=profile.mounts + (('gate_channel_directory', '/run/hub-gate', 'ro', '/operator/gate'),))
    container_id = 'c' * 64
    body = container_inspect(image, profile, source, container_id=container_id)
    body['HostConfig']['Mounts'][0].pop('ReadOnly')
    body['Mounts'] = body['Mounts'][:1]; body['Mounts'][0]['Mode'] = ''
    body['HostConfig']['Mounts'].append({'Type': 'bind', 'Source': '/operator/gate',
                                         'Target': '/run/hub-gate', 'ReadOnly': True})
    body['Mounts'].append({'Type': 'bind', 'Source': '/operator/gate', 'Destination': '/run/hub-gate',
                           'Mode': 'ro', 'RW': False, 'Propagation': 'rprivate'})
    body['State'].update(Running=False, Pid=0, StartedAt='0001-01-01T00:00:00Z')
    requests = []
    def handler(request):
        requests.append((request.method, request.url.path))
        if request.url.path.endswith('/info'):
            return httpx.Response(200, json={'ID': manifest.runner_binding.daemon_identity,
                'OSType': 'linux', 'Architecture': 'x86_64'})
        if request.method == 'GET' and request.url.path.endswith('/archive'):
            assert request.url.params['path'] == STATIC_ENTRYPOINT[0]
            return httpx.Response(200, content=archive(elf(machine=183) if control['bad_archive'] else elf()))
        if request.method == 'GET' and request.url.path.endswith('/json'):
            return httpx.Response(200, json=body) if container_id in request.url.path else httpx.Response(404)
        if request.method == 'POST' and request.url.path.endswith('/create'):
            mounts = __import__('json').loads(request.content)['HostConfig']['Mounts']
            assert mounts[-1] == {'Type': 'bind', 'Source': backend._gate_mount_verifier.source_directory,
                                  'Target': '/run/hub-gate', 'ReadOnly': True}
            return httpx.Response(201, json={'Id': container_id, 'Warnings': []})
        if request.method == 'POST' and request.url.path.endswith('/start'):
            body['State'].update(Status='running', Running=True, Pid=123, StartedAt='2026-10-31T00:00:00Z')
            return httpx.Response(204)
        raise AssertionError('unexpected request')
    backend = DockerEngineBackend(binding(manifest, tmp_path), gate_mount_verifier=verifier,
                                  test_transport=httpx.MockTransport(handler))
    defaults = ImageDefaults(image.image_config_id, image.platform, STATIC_ENTRYPOINT, (), ())
    backend._images[image.runnable_reference] = (image, image.image_config, defaults)
    backend.inspect_image = lambda _image, _timeout: defaults
    plan = ContainerPlan('hub-pilot-fixture-01234567-89a-candidate', image, image.runnable_reference, False, profile)
    try: yield backend, plan, body, requests, calls, control
    finally: backend.close()


def test_gated_create_inspect_and_start_hash_exact_static_binary(rig):
    backend, plan, _body, requests, calls, _control = rig
    container_id = backend.create_container(plan, str(uuid4()), 5000)
    backend.start_container(container_id, str(uuid4()), 5000)
    archive_index = next(i for i, (method, path) in enumerate(requests) if method == 'GET' and path.endswith('/archive'))
    start_index = next(i for i, (method, path) in enumerate(requests) if method == 'POST' and path.endswith('/start'))
    assert archive_index < start_index and len(calls) >= 6


@pytest.mark.parametrize('fault', ['wrong_source', 'wrong_target', 'writable', 'extra', 'legacy_image', 'legacy_image_config'])
def test_bad_gate_plan_is_rejected_before_docker_io(rig, fault):
    backend, plan, _body, requests, _calls, _control = rig
    mounts = list(plan.profile.mounts)
    if fault == 'wrong_source': mounts[-1] = ('gate_channel_directory', '/run/hub-gate', 'ro', '/other')
    if fault == 'wrong_target': mounts[-1] = ('gate_channel_directory', '/other', 'ro', '/operator/gate')
    if fault == 'writable': mounts[-1] = ('gate_channel_directory', '/run/hub-gate', 'rw', '/operator/gate')
    if fault == 'extra': mounts.insert(1, ('extra', '/tmp/extra', 'ro', '/other'))
    profile = replace(plan.profile, mounts=tuple(mounts),
        entrypoint=('/usr/local/bin/hub',) if fault == 'legacy_image' else plan.profile.entrypoint)
    image = (replace(plan.image, image_config={'config': {'Entrypoint': ['/usr/local/bin/hub'], 'Cmd': []}})
             if fault == 'legacy_image_config' else plan.image)
    with pytest.raises(DaemonBackendError):
        backend.create_container(replace(plan, profile=profile, image=image), str(uuid4()), 5000)
    assert requests == []


def test_unavailable_signed_observation_denies_before_docker_io(rig):
    backend, plan, _body, requests, _calls, control = rig
    control['fail_observation'] = True
    with pytest.raises(DaemonBackendError, match='hold original operation'):
        backend.create_container(plan, str(uuid4()), 5000)
    assert requests == []


@pytest.mark.parametrize('fail_at,created', [(2, False), (3, True)])
def test_observation_loss_around_create_is_never_retried(rig, fail_at, created):
    backend, plan, _body, requests, _calls, control = rig
    control['fail_at'] = fail_at
    with pytest.raises(DaemonBackendError, match='hold original operation'):
        backend.create_container(plan, str(uuid4()), 5000)
    assert any(method == 'POST' and path.endswith('/create') for method, path in requests) is created
    assert not any(method == 'POST' and path.endswith('/start') for method, path in requests)


@pytest.mark.parametrize('fault', ['wrong_source', 'writable', 'missing_readonly'])
def test_daemon_inspect_gate_mount_mismatch_holds_after_create(rig, fault):
    backend, plan, body, requests, _calls, _control = rig
    if fault == 'wrong_source': body['Mounts'][-1]['Source'] = '/other'
    if fault == 'writable': body['Mounts'][-1]['RW'] = True
    if fault == 'missing_readonly': body['HostConfig']['Mounts'][-1].pop('ReadOnly')
    with pytest.raises(DaemonBackendError): backend.create_container(plan, str(uuid4()), 5000)
    assert any(method == 'POST' and path.endswith('/create') for method, path in requests)
    assert not any(method == 'POST' and path.endswith('/start') for method, path in requests)


def test_wrong_bootstrap_never_starts(rig):
    backend, plan, _body, requests, _calls, control = rig
    container_id = backend.create_container(plan, str(uuid4()), 5000)
    control['bad_archive'] = True
    with pytest.raises(DaemonBackendError): backend.start_container(container_id, str(uuid4()), 5000)
    assert not any(method == 'POST' and path.endswith('/start') for method, path in requests)
