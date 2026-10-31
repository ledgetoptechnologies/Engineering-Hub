"""Closed, root-protected application-gate proposal; it does not enable launch.

The caller must supply the independently authenticated supervisor profile digest
and a separately verified build record. Parsing these bytes does not establish
process identity, descriptor custody, storage isolation or image provenance.
The existing application backend does not accept the additional socket mount.
The controller public key is an independently enrolled proposed pin: this
parser does not compare it to a running controller or supervisor signing key.
The bootstrap digest is enrolled metadata, not a measurement of image bytes.
Any future runtime must independently verify the actual controller key and
bootstrap bytes against those pins before authorization.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from engineering_hub.container_pilot_manifest import PilotManifest
from .build_record import VerifiedImage
from .launch_supervisor import LaunchSupervisor, _path, _root_file, _strict
from .runtime_identity import ExecutorReleaseEnrollment

_SHA = re.compile(r'sha256:[0-9a-f]{64}\Z')
_KEY = re.compile(r'[0-9a-f]{64}\Z')
_SOCKET_TARGET = '/run/hub-gate/controller.sock'
_ENTRYPOINT = ('/opt/venv/bin/python', '-I', '-B', '-m',
               'hub_container_executor.application_mount_gate')
_FIELDS = frozenset({'schema', 'manifest_digest', 'supervisor_profile_digest', 'role',
    'runnable_digest', 'platform_manifest_digest', 'image_config_id', 'source_commit',
    'build_record_digest', 'bootstrap_digest', 'controller_public_key',
    'socket_target', 'request_timeout_ms'})


class ApplicationGateEnrollmentError(ValueError):
    def __init__(self):
        super().__init__('application gate enrollment unavailable')


@dataclass(frozen=True)
class ApplicationGateEnrollment:
    """Diagnostic snapshot, not an authorization capability.

    Public construction or dataclass replacement is possible. A future caller
    must re-run the loader against trusted inputs at each admission boundary;
    ``check`` only detects replacement of the original file identity.
    """
    path: str
    identity: tuple
    manifest_digest: str
    supervisor_profile_digest: str
    role: str
    runnable_digest: str
    platform_manifest_digest: str
    image_config_id: str
    source_commit: str
    build_record_digest: str
    bootstrap_digest: str
    controller_public_key: bytes
    socket_target: str
    request_timeout_ms: int

    def check(self):
        try:
            if _root_file(_path(self.path), 65536)[1] != self.identity:
                raise ValueError()
        except Exception:
            raise ApplicationGateEnrollmentError() from None


def load_application_gate_enrollment(path, *, manifest, image, supervisor):
    """Validate exact enrollment against trusted caller-provided release bindings.

    Root-owned file and ancestry are checked with the supervisor's existing
    no-follow reader. The supplied supervisor must be the existing trusted,
    enrolled owner; the VerifiedImage must come from existing trust checks.
    A digest match alone is not proof of image entrypoint or bootstrap contents.
    Neither the controller public key nor bootstrap digest is measured here.
    """
    try:
        if (type(path) is not str or type(manifest) is not PilotManifest
                or type(image) is not VerifiedImage or type(supervisor) is not LaunchSupervisor
                or type(supervisor.release) is not ExecutorReleaseEnrollment):
            raise ValueError()
        supervisor._guard()
        supervisor_profile_digest = supervisor.profile.digest()
        if supervisor_profile_digest != supervisor.release.launch_profile_digest:
            raise ValueError()
        raw, identity = _root_file(_path(path), 65536)
        data = _strict(raw, 65536)
        if (type(data) is not dict or set(data) != _FIELDS
                or data['schema'] != 'engineering-hub.application-gate-enrollment.v1'
                or data['role'] not in ('candidate', 'rollback')
                or data['role'] != image.role
                or data['manifest_digest'] != manifest.digest()
                or data['supervisor_profile_digest'] != supervisor_profile_digest
                or data['socket_target'] != _SOCKET_TARGET
                or type(data['request_timeout_ms']) is not int
                or not 100 <= data['request_timeout_ms'] <= 5000):
            raise ValueError()
        selected = getattr(manifest, data['role'])
        for field in ('manifest_digest', 'supervisor_profile_digest', 'runnable_digest',
                      'platform_manifest_digest', 'image_config_id', 'build_record_digest', 'bootstrap_digest'):
            if type(data[field]) is not str or not _SHA.fullmatch(data[field]):
                raise ValueError()
        if (data['runnable_digest'] != image.runnable_reference
                or data['platform_manifest_digest'] != image.platform_manifest_digest
                or data['image_config_id'] != image.image_config_id
                or data['image_config_id'] != selected.image_config_id
                or data['source_commit'] != image.source_commit
                or data['source_commit'] != selected.source_commit
                or data['build_record_digest'] != image.record_digest
                or image.platform != manifest.runner_binding.platform
                or type(data['controller_public_key']) is not str
                or not _KEY.fullmatch(data['controller_public_key'])
                or data['controller_public_key'] == '0' * 64):
            raise ValueError()
        # An image-default gate is a distinct image contract; a command override
        # or a legacy Hub image cannot satisfy this versioned enrollment.
        config = image.image_config.get('config')
        if (type(config) is not dict or config.get('Entrypoint') != list(_ENTRYPOINT)
                or config.get('Cmd') not in ([], None)):
            raise ValueError()
        supervisor._guard()
        if supervisor.profile.digest() != supervisor_profile_digest:
            raise ValueError()
        return ApplicationGateEnrollment(path, identity, data['manifest_digest'],
            data['supervisor_profile_digest'], data['role'], data['runnable_digest'],
            data['platform_manifest_digest'], data['image_config_id'], data['source_commit'],
            data['build_record_digest'], data['bootstrap_digest'],
            bytes.fromhex(data['controller_public_key']), data['socket_target'], data['request_timeout_ms'])
    except Exception:
        raise ApplicationGateEnrollmentError() from None
