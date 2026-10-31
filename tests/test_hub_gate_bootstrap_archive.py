"""Synthetic static bootstrap and Docker responses; no provenance or host proof."""
import hashlib
import io
import struct
import tarfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from hub_container_executor import application_gate_enrollment as gate
from hub_container_executor.backend import ImageDefaults
from engineering_hub.executor_launch_profile import ExecutorLaunchProfile
from hub_container_executor.daemon_backend import DaemonBackendError, DockerEngineBackend, _static_gate_archive
from tests.test_hub_application_gate_enrollment import raw, vector
from tests.test_hub_container_executor_daemon_backend import binding, container_inspect, make_profile, safe_generation
from tests.test_executor_launch_profile import gate_profile


def elf(*, machine=62, segment=1):
    ident = b'\x7fELF\x02\x01\x01' + bytes(9)
    header = struct.pack('<16sHHIQQQIHHHHHH', ident, 2, machine, 1, 0x400078, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    program = struct.pack('<IIQQQQQQ', segment, 5, 0, 0x400000, 0, 128, 128, 4096)
    return header + program + bytes(8)


def archive(content, *, name='hub-application-gate', kind=tarfile.REGTYPE, mode=0o755):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as tar:
        info = tarfile.TarInfo(name); info.type = kind; info.mode = mode
        info.size = len(content) if kind == tarfile.REGTYPE else 0
        tar.addfile(info, io.BytesIO(content) if info.size else None)
    return output.getvalue()


def digest(data): return 'sha256:' + hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize('fault', ['bad_hash', 'interpreter', 'dynamic', 'wrong_arch', 'bad_name',
    'symlink', 'writable', 'special_mode', 'owner_only', 'truncated', 'trailing', 'extra_member', 'non_elf'])
def test_exact_archive_and_static_elf_reject_ambiguity(fault):
    binary = elf(); payload = archive(binary); expected = digest(binary)
    if fault == 'bad_hash': expected = 'sha256:' + '0' * 64
    if fault in ('interpreter', 'dynamic'):
        binary = elf(segment=3 if fault == 'interpreter' else 2); payload = archive(binary); expected = digest(binary)
    if fault == 'wrong_arch': binary = elf(machine=183); payload = archive(binary); expected = digest(binary)
    if fault == 'bad_name': payload = archive(binary, name='../hub-application-gate')
    if fault == 'symlink': payload = archive(binary, kind=tarfile.SYMTYPE)
    if fault == 'writable': payload = archive(binary, mode=0o777)
    if fault == 'special_mode': payload = archive(binary, mode=0o4755)
    if fault == 'owner_only': payload = archive(binary, mode=0o700)
    if fault == 'truncated': payload = payload[:600]
    if fault == 'trailing': payload = payload[:-1] + b'x'
    if fault == 'extra_member': payload = payload[:1024] + archive(binary)
    if fault == 'non_elf': binary = bytes(128); payload = archive(binary); expected = digest(binary)
    with pytest.raises(DaemonBackendError): _static_gate_archive(payload, expected, 'amd64')


@pytest.mark.parametrize('fault', ['none', 'v3', 'v4', 'wrong_binary', 'changed_container', 'missing_enrollment', 'old_schema', 'http_error'])
def test_created_container_archive_uses_protected_v2_policy_and_never_starts(tmp_path, monkeypatch, fault):
    manifest, image, supervisor, policy = vector()
    binary = elf()
    image = replace(image, image_config={'config': {'Entrypoint': list(gate.STATIC_ENTRYPOINT), 'Cmd': [], 'Env': []}})
    policy.update(schema=gate.STATIC_SCHEMA, bootstrap_digest=digest(binary))
    if fault in ('v3', 'v4'):
        supervisor.profile = ExecutorLaunchProfile.model_validate(gate_profile())
        supervisor.release = replace(supervisor.release, launch_profile_digest=supervisor.profile.digest())
        policy.update(schema=gate.SOCKET_SCHEMA, socket_source_directory='/operator/hub-pilot-synthetic/gate-control',
                      supervisor_profile_digest=supervisor.profile.digest(),
                      consumer_host_uid=110001, consumer_host_gid=110001)
        supervisor._enrollment_path = Path('/operator/protected/enrollment')
        supervisor._key_path = Path('/operator/protected/key')
        supervisor._journal = SimpleNamespace(path=Path('/operator/protected/journal'))
        supervisor._mounts = {mount.role: mount for mount in supervisor.profile.mounts}
        if fault == 'v4':
            from tests.test_hub_gate_socket_supervisor import v4_policy
            v4_policy(supervisor, policy)
            policy.update(schema=gate.COMPLETION_SCHEMA, application_executable_digest='sha256:' + 'a' * 64,
                          health_contract=gate.COMPLETION_HEALTH_CONTRACT)
    if fault == 'old_schema': policy['schema'] = 'engineering-hub.application-gate-enrollment.v1'
    def root_file(*_):
        if fault == 'missing_enrollment': raise FileNotFoundError()
        return raw(policy), ((1, 2), 'synthetic-hash')
    monkeypatch.setattr(gate, '_root_file', root_file)
    source = safe_generation(tmp_path)
    profile = make_profile(image)
    body = container_inspect(image, profile, source)
    body['HostConfig']['Mounts'][0].pop('ReadOnly')
    body['Mounts'] = body['Mounts'][:1]; body['Mounts'][0]['Mode'] = ''
    body['State'].update(Running=False, Pid=0, StartedAt='0001-01-01T00:00:00Z')
    requests, inspections = [], []
    def handler(request):
        requests.append((request.method, request.url.path))
        if request.url.path.endswith('/info'):
            return httpx.Response(200, json={'ID': manifest.runner_binding.daemon_identity,
                'OSType': 'linux', 'Architecture': 'x86_64'})
        if request.url.path.endswith('/json'):
            inspections.append(1)
            if fault == 'changed_container' and len(inspections) > 1:
                body['State']['Status'] = 'running'
            return httpx.Response(200, json=body)
        assert request.url.path.endswith('/archive') and request.url.params['path'] == gate.STATIC_ENTRYPOINT[0]
        if fault == 'http_error': return httpx.Response(503)
        return httpx.Response(200, content=archive(elf(machine=183) if fault == 'wrong_binary' else binary))
    backend = DockerEngineBackend(binding(manifest, tmp_path), test_transport=httpx.MockTransport(handler))
    container_id = 'c' * 64
    defaults = ImageDefaults(image.image_config_id, image.platform, gate.STATIC_ENTRYPOINT, (), ())
    backend._images[image.runnable_reference] = (image, image.image_config, defaults)
    backend._container_images[container_id] = image
    backend._container_profiles[container_id] = profile
    backend._container_generations[container_id] = profile.state_generation_id
    try:
        kwargs = dict(manifest=manifest, image=image, supervisor=supervisor,
                      enrollment_file='/root-owned/enrollment.json', timeout_ms=5000)
        if fault in ('none', 'v3', 'v4'):
            assert backend.inspect_created_gate_bootstrap(container_id, **kwargs) is None
            assert sum(path.endswith('/json') for _, path in requests) == 2
        else:
            with pytest.raises(DaemonBackendError, match='hold original operation'):
                backend.inspect_created_gate_bootstrap(container_id, **kwargs)
        assert all(method == 'GET' for method, _ in requests)
    finally:
        backend.close()
