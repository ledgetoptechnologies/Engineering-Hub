"""Closed profile parsing only; synthetic image records are not provenance."""
import json
import os
from dataclasses import replace

import pytest

from engineering_hub.container_pilot_manifest import PilotManifest
from hub_container_executor import application_gate_enrollment as gate
from hub_container_executor.build_record import VerifiedImage
from hub_container_executor.launch_supervisor import LaunchSupervisor
from hub_container_executor.runtime_identity import ExecutorReleaseEnrollment
from engineering_hub.executor_launch_profile import ExecutorLaunchProfile
from tests.test_hub_container_pilot_manifest import make_manifest_data
from tests.test_hub_container_executor_launch_supervisor import profile_data


def vector():
    manifest = PilotManifest.from_bytes(json.dumps(make_manifest_data()).encode())
    image = VerifiedImage(role='candidate', source_commit=manifest.candidate.source_commit,
        runnable_reference='sha256:' + '1' * 64, runnable_media_type='application/vnd.oci.image.manifest.v1+json',
        platform_manifest_digest='sha256:' + '2' * 64, image_config_id=manifest.candidate.image_config_id,
        platform=manifest.runner_binding.platform, image_config={'config': {
            'Entrypoint': list(gate._ENTRYPOINT), 'Cmd': []}}, record_digest='sha256:' + '3' * 64)
    profile = ExecutorLaunchProfile.model_validate(profile_data())
    supervisor = object.__new__(LaunchSupervisor)
    supervisor.profile = profile
    supervisor.release = ExecutorReleaseEnrollment(issuer_id='fixture-issuer', public_key=b'1' * 32,
        implementation_commit='4' * 40, runnable_digest='sha256:' + '4' * 64,
        platform_manifest_digest='sha256:' + '4' * 64, image_config_id='sha256:' + '4' * 64,
        platform=manifest.runner_binding.platform, launch_profile_digest=profile.digest())
    supervisor._guard = lambda: None  # This fixture does not establish enrollment authority.
    data = {'schema': 'engineering-hub.application-gate-enrollment.v1',
        'manifest_digest': manifest.digest(), 'supervisor_profile_digest': profile.digest(),
        'role': image.role, 'runnable_digest': image.runnable_reference,
        'platform_manifest_digest': image.platform_manifest_digest, 'image_config_id': image.image_config_id,
        'source_commit': image.source_commit, 'build_record_digest': image.record_digest,
        'bootstrap_digest': 'sha256:' + '5' * 64, 'controller_public_key': '6' * 64,
        'socket_target': '/run/hub-gate/controller.sock', 'request_timeout_ms': 2000}
    return manifest, image, supervisor, data


def raw(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def load(monkeypatch, manifest, image, supervisor, data):
    monkeypatch.setattr(gate, '_root_file', lambda *_: (raw(data), ((1, 2), 'content-hash')))
    return gate.load_application_gate_enrollment('/root-owned/enrollment.json', manifest=manifest,
        image=image, supervisor=supervisor)


def test_closed_enrollment_binds_all_existing_release_inputs(monkeypatch):
    manifest, image, profile, data = vector()
    enrolled = load(monkeypatch, manifest, image, profile, data)
    enrolled.check()
    assert enrolled.controller_public_key == bytes.fromhex(data['controller_public_key'])
    assert enrolled.manifest_digest == manifest.digest()
    monkeypatch.setattr(gate, '_root_file', lambda *_: (b'replacement', ((1, 3), 'other')))
    with pytest.raises(gate.ApplicationGateEnrollmentError): enrolled.check()


@pytest.mark.parametrize('field,value', [
    ('schema', 'legacy'), ('role', 'rollback'), ('manifest_digest', 'sha256:' + '7' * 64),
    ('supervisor_profile_digest', 'sha256:' + '7' * 64), ('runnable_digest', 'sha256:' + '7' * 64),
    ('platform_manifest_digest', 'sha256:' + '7' * 64), ('image_config_id', 'sha256:' + '7' * 64),
    ('source_commit', '7' * 40), ('build_record_digest', 'sha256:' + '7' * 64),
    ('bootstrap_digest', 'bogus'), ('controller_public_key', '0' * 64),
    ('socket_target', '/var/run/docker.sock'), ('request_timeout_ms', True),
    ('request_timeout_ms', 5001), ('unexpected', 'field'),
])
def test_rejects_mismatched_or_open_profile(monkeypatch, field, value):
    manifest, image, profile, data = vector()
    data[field] = value
    with pytest.raises(gate.ApplicationGateEnrollmentError): load(monkeypatch, manifest, image, profile, data)


def test_legacy_image_absence_or_bad_manifest_cannot_enroll(monkeypatch):
    manifest, image, profile, data = vector()
    legacy = replace(image, image_config={'config': {'Entrypoint': ['/usr/local/bin/hub'], 'Cmd': ['serve']}})
    with pytest.raises(gate.ApplicationGateEnrollmentError): load(monkeypatch, manifest, legacy, profile, data)
    with pytest.raises(gate.ApplicationGateEnrollmentError):
        gate.load_application_gate_enrollment(None, manifest=manifest, image=image, supervisor=profile)
    assert manifest.schema_version == 'container_pilot_manifest_v1'


@pytest.mark.skipif(os.name != 'posix' or os.geteuid() != 0, reason='requires disposable root-owned fixture')
def test_actual_root_file_rejects_replacement_and_insecure_mode(tmp_path):
    manifest, image, profile, data = vector()
    root = tmp_path / 'protected'; root.mkdir(mode=0o700)
    path = root / 'policy.json'; path.write_bytes(raw(data)); path.chmod(0o600)
    enrolled = gate.load_application_gate_enrollment(str(path), manifest=manifest, image=image,
        supervisor=profile)
    enrolled.check()
    path.chmod(0o644)
    with pytest.raises(gate.ApplicationGateEnrollmentError): enrolled.check()
    with pytest.raises(gate.ApplicationGateEnrollmentError):
        gate.load_application_gate_enrollment(str(path), manifest=manifest, image=image,
            supervisor=profile)


def test_supervisor_profile_must_match_release_and_guard(monkeypatch):
    manifest, image, supervisor, data = vector()
    supervisor.release = replace(supervisor.release, launch_profile_digest='sha256:' + '7' * 64)
    with pytest.raises(gate.ApplicationGateEnrollmentError):
        load(monkeypatch, manifest, image, supervisor, data)
    supervisor.release = replace(supervisor.release, launch_profile_digest=supervisor.profile.digest())
    def reject(): raise RuntimeError('private guard detail')
    supervisor._guard = reject
    with pytest.raises(gate.ApplicationGateEnrollmentError, match='application gate enrollment unavailable'):
        load(monkeypatch, manifest, image, supervisor, data)
