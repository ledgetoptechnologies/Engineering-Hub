"""Root-protected host approval for one immutable app-gate operation.

Root enrollment is an explicit operator trust anchor, not a build signature or
proof of host isolation. No resource or image is created by this loader.
"""
import base64
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass

from engineering_hub.container_pilot_manifest import PilotManifest
from .build_enrollment import BuildEnrollment, OperatorEnrolledBuildVerifier
from .build_record import verify_image_record
from .launch_supervisor import _canonical, _path, _root_file, _strict

MAX_BYTES = 24 * 1024 * 1024
SCHEMA = 'engineering-hub.host-gate-operation.v1'


class GateHostEnrollmentError(ValueError):
    def __init__(self): super().__init__('host gate enrollment unavailable')


@dataclass(frozen=True)
class GateHostEnrollment:
    path: str
    identity: tuple
    manifest: PilotManifest
    image: object
    application_enrollment_file: str
    controller_key_file: str

    def check(self):
        try:
            if _root_file(_path(self.path), MAX_BYTES)[1] != self.identity: raise ValueError()
        except Exception:
            raise GateHostEnrollmentError() from None


class _Store:
    def __init__(self, values): self.values = values
    def get(self, digest):
        value = self.values[digest]
        if 'sha256:' + hashlib.sha256(value).hexdigest() != digest: raise ValueError()
        return value


def load_gate_host_enrollment(path):
    """Verify bounded canonical root-owned approval and the full OCI chain."""
    try:
        if type(path) is not str: raise ValueError()
        raw, identity = _root_file(_path(path), MAX_BYTES)
        data = _strict(raw, MAX_BYTES)
        fields = {'schema', 'manifest', 'role', 'build_enrollment', 'record', 'metadata',
                  'application_enrollment_file', 'controller_key_file'}
        if type(data) is not dict or set(data) != fields or data['schema'] != SCHEMA or _canonical(data) != raw:
            raise ValueError()
        manifest = PilotManifest.from_bytes(_canonical(data['manifest']))
        role = data['role']
        if type(role) is not str or role not in ('candidate', 'rollback'): raise ValueError()
        build = data['build_enrollment']
        if type(build) is not dict or set(build) != {'manifest_digest', 'role', 'record_digest', 'builder_id', 'invocation_id'}:
            raise ValueError()
        approval = BuildEnrollment(**build)
        if approval.manifest_digest != manifest.digest() or approval.role != role: raise ValueError()
        record = _canonical(data['record'])
        if len(record) > 65536 or 'sha256:' + hashlib.sha256(record).hexdigest() != approval.record_digest: raise ValueError()
        metadata = data['metadata']
        if type(metadata) is not list or not 1 <= len(metadata) <= 64: raise ValueError()
        blobs, total = {}, 0
        for entry in metadata:
            if (type(entry) is not dict or set(entry) != {'digest', 'base64'} or type(entry['digest']) is not str
                    or not re.fullmatch(r'sha256:[0-9a-f]{64}', entry['digest']) or entry['digest'] in blobs
                    or type(entry['base64']) is not str): raise ValueError()
            value = base64.b64decode(entry['base64'], validate=True)
            if base64.b64encode(value).decode() != entry['base64'] or not 0 < len(value) <= 1024 * 1024: raise ValueError()
            total += len(value)
            if total > 16 * 1024 * 1024 or 'sha256:' + hashlib.sha256(value).hexdigest() != entry['digest']: raise ValueError()
            blobs[entry['digest']] = value
        image = verify_image_record(manifest, role, _Store({approval.record_digest: record}), _Store(blobs),
                                    OperatorEnrolledBuildVerifier(enrollments=(approval,)))
        app, key = str(_path(data['application_enrollment_file'])), str(_path(data['controller_key_file']))
        if app == key or app == path or key == path: raise ValueError()
        return GateHostEnrollment(path, identity, manifest, image, app, key)
    except Exception:
        raise GateHostEnrollmentError() from None


def preflight_gate_host_enrollment(operation, supervisor):
    """Bind root-approved operation to the exact pinned runner and controller."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from .application_gate_enrollment import COMPLETION_SCHEMA, load_application_gate_enrollment
    from .launch_supervisor import LaunchSupervisor
    from .configuration_commitment import configuration_commitment
    try:
        if type(operation) is not GateHostEnrollment or type(supervisor) is not LaunchSupervisor:
            raise ValueError()
        operation.check()
        supervisor._guard()
        mount = supervisor._mounts['config']; path = _path(mount.source)
        if path.resolve(strict=True) != path: raise ValueError()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            info, named = os.fstat(fd), os.lstat(path)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != mount.uid
                    or info.st_gid != mount.gid or stat.S_IMODE(info.st_mode) != mount.mode
                    or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
                    or not 0 < info.st_size <= 65536): raise ValueError()
            raw = os.read(fd, 65537)
            if len(raw) != info.st_size: raise ValueError()
            after = os.fstat(fd)
            named_after = os.lstat(path)
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                    info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) or (
                    named_after.st_dev, named_after.st_ino) != (info.st_dev, info.st_ino) or path.resolve(strict=True) != path:
                raise ValueError()
        finally:
            os.close(fd)
        commitment = configuration_commitment(raw)
        parsed = supervisor._runner_configuration(raw)
        if (supervisor.profile.gate_ledger_channel != 'supervisor-listener-v1'
                or commitment.raw_digest != supervisor._configuration_raw_digest
                or commitment.content_digest != mount.content_digest or commitment.release != supervisor.release
                or parsed.manifest.digest() != operation.manifest.digest()): raise ValueError()
        app = load_application_gate_enrollment(operation.application_enrollment_file,
            manifest=operation.manifest, image=operation.image, supervisor=supervisor)
        key, _ = _root_file(_path(operation.controller_key_file), 32)
        if (app.schema != COMPLETION_SCHEMA or len(key) != 32
                or Ed25519PrivateKey.from_private_bytes(key).public_key().public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw) != app.controller_public_key):
            raise ValueError()
        operation.check()
    except Exception:
        raise GateHostEnrollmentError() from None
