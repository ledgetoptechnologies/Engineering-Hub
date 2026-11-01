"""Synthetic root approval and real build-record/OCI validation."""
import base64
import json
import os
import hashlib
from types import SimpleNamespace

import pytest

from hub_container_executor import gate_host_enrollment as host
from tests.test_hub_container_executor_build_enrollment import fixture
from tests.test_hub_container_executor_build_record import canonical
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def document():
    manifest, raw, blobs, record, approval = fixture()
    return {'schema': host.SCHEMA, 'manifest': json.loads(manifest.canonical_bytes()), 'role': 'candidate',
        'build_enrollment': dict(manifest_digest=approval.manifest_digest, role=approval.role,
            record_digest=approval.record_digest, builder_id=approval.builder_id, invocation_id=approval.invocation_id),
        'record': record, 'metadata': [{'digest': digest, 'base64': base64.b64encode(value).decode()}
                                     for digest, value in blobs.items()],
        'application_enrollment_file': '/root-private/app-policy', 'controller_key_file': '/root-private/key'}


@pytest.mark.parametrize('fault', ['none', 'blob', 'approval', 'role', 'extra', 'noncanonical'])
def test_root_approved_record_is_verified_not_merely_labeled(monkeypatch, fault):
    data = document()
    if fault == 'blob': data['metadata'][0]['base64'] = base64.b64encode(b'wrong').decode()
    elif fault == 'approval': data['build_enrollment']['builder_id'] = 'another-builder'
    elif fault == 'role': data['role'] = 'rollback'
    elif fault == 'extra': data['unexpected'] = True
    payload = canonical(data) + (b' ' if fault == 'noncanonical' else b'')
    monkeypatch.setattr(host, '_root_file', lambda *_: (payload, ((1, 2), 'pinned')))
    if fault == 'none':
        result = host.load_gate_host_enrollment('/root-private/operation')
        assert result.manifest.digest() == data['build_enrollment']['manifest_digest']
        assert result.image.record_digest == data['build_enrollment']['record_digest']
        result.check()
    else:
        with pytest.raises(host.GateHostEnrollmentError): host.load_gate_host_enrollment('/root-private/operation')


@pytest.mark.skipif(os.name != 'posix' or os.geteuid() != 0, reason='requires disposable root-owned fixture')
def test_actual_root_file_replacement_is_detected(tmp_path):
    root = tmp_path / 'private'; root.mkdir(mode=0o700)
    path = root / 'operation'; path.write_bytes(canonical(document())); path.chmod(0o600)
    result = host.load_gate_host_enrollment(str(path))
    path.write_bytes(canonical(document()))
    with pytest.raises(host.GateHostEnrollmentError): result.check()


@pytest.mark.skipif(os.name != 'posix' or os.geteuid() != 0, reason='requires disposable root-owned fixture')
def test_preflight_reads_pinned_config_before_issuer_socket_exists(tmp_path, monkeypatch):
    from hub_container_executor import application_gate_enrollment as app
    from hub_container_executor import configuration_commitment as commitments
    from hub_container_executor.launch_supervisor import LaunchSupervisor
    root = tmp_path / 'private'; root.mkdir(mode=0o700)
    path = root / 'operation'; path.write_bytes(canonical(document())); path.chmod(0o600)
    operation = host.load_gate_host_enrollment(str(path))
    config = root / 'runner'; config.write_bytes(b'config'); config.chmod(0o600)
    key = Ed25519PrivateKey.generate()
    key_bytes = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    key_path = root / 'controller'; key_path.write_bytes(key_bytes); key_path.chmod(0o600)
    operation = host.GateHostEnrollment(operation.path, operation.identity, operation.manifest, operation.image,
                                        str(root / 'app'), str(key_path))
    supervisor = object.__new__(LaunchSupervisor)
    supervisor.profile = SimpleNamespace(gate_ledger_channel='supervisor-listener-v1')
    supervisor.release = object(); supervisor._guard = lambda: None
    supervisor._mounts = {'config': SimpleNamespace(source=str(config), uid=os.getuid(), gid=os.getgid(), mode=0o600,
                                                    content_digest='content')}
    expected = hashlib.sha256(b'config').hexdigest(); supervisor._configuration_raw_digest = expected
    monkeypatch.setattr(commitments, 'configuration_commitment', lambda raw: SimpleNamespace(
        raw_digest=hashlib.sha256(raw).hexdigest(), content_digest='content', release=supervisor.release))
    supervisor._runner_configuration = lambda _: SimpleNamespace(manifest=operation.manifest)
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(app, 'load_application_gate_enrollment', lambda *_, **__: SimpleNamespace(
        schema=app.COMPLETION_SCHEMA, controller_public_key=public))
    host.preflight_gate_host_enrollment(operation, supervisor)
    replacement = root / 'replacement'; replacement.write_bytes(b'config'); replacement.chmod(0o600)
    before = os.stat(config); original_read = os.read; swapped = []
    def read_and_replace(fd, count):
        result = original_read(fd, count)
        info = os.fstat(fd)
        if not swapped and (info.st_dev, info.st_ino) == (before.st_dev, before.st_ino):
            os.replace(replacement, config); swapped.append(True)
        return result
    monkeypatch.setattr(host.os, 'read', read_and_replace)
    with pytest.raises(host.GateHostEnrollmentError): host.preflight_gate_host_enrollment(operation, supervisor)
    assert swapped and config.read_bytes() == b'config'
