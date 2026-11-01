"""Explicit root-enrolled one-shot private custodian worker; no publication."""
import argparse
from dataclasses import dataclass
import hashlib
import os
import re
import socket
import stat
import struct
import sys
import time
from uuid import UUID, uuid5

from engineering_hub.executor_launch_profile import verify_profile_bytes, _proc_map_rows
from .launch_supervisor import _canonical, _path, _root_file, _root_parent, _strict, _Process
from .state_custodian_admission import CustodianAdmissionJournal

SCHEMA = 'engineering-hub.custodian-worker-operation.v1'
_SHA = re.compile(r'sha256:[0-9a-f]{64}\Z')
_HEX = re.compile(r'[0-9a-f]{64}\Z')


class CustodianWorkerError(RuntimeError):
    def __init__(self): super().__init__('custodian worker unavailable; preserve original evidence')


@dataclass(frozen=True)
class CustodianOperation:
    path: str
    identity: tuple
    profile: object
    profile_digest: str
    supervisor_public_key: str
    operation_id: str
    command_id: str
    run_id: str
    target_id: str
    fencing_generation: int
    manifest_digest: str
    source_backup_digest: str
    role: str
    timeout_ms: int

    def check(self):
        try:
            if _root_file(_path(self.path), 65536)[1] != self.identity: raise ValueError()
        except Exception:
            raise CustodianWorkerError() from None


def load_custodian_operation(path):
    """Load exact root-approved inputs; root protection is the trust anchor."""
    try:
        raw, pin = _root_file(_path(path), 65536)
        value = _strict(raw, 65536)
        fields = {'schema', 'profile_json', 'profile_digest', 'supervisor_public_key', 'operation_id', 'command_id',
                  'run_id', 'target_id', 'fencing_generation', 'manifest_digest', 'source_backup_digest', 'role', 'timeout_ms'}
        if (type(value) is not dict or set(value) != fields or _canonical(value) != raw or value['schema'] != SCHEMA
                or type(value['profile_json']) is not str or type(value['profile_digest']) is not str
                or type(value['supervisor_public_key']) is not str or not _HEX.fullmatch(value['supervisor_public_key'])
                or value['supervisor_public_key'] == '0' * 64 or value['target_id'] != 'engineering-hub:staging'
                or type(value['fencing_generation']) is not int or not 0 < value['fencing_generation'] < 2**63
                or any(type(value[k]) is not str or not _SHA.fullmatch(value[k]) for k in ('manifest_digest', 'source_backup_digest'))
                or value['role'] not in ('candidate', 'rollback') or type(value['timeout_ms']) is not int
                or not 1 <= value['timeout_ms'] <= 5000): raise ValueError()
        for name in ('operation_id', 'command_id', 'run_id'):
            if type(value[name]) is not str or str(UUID(value[name])) != value[name]: raise ValueError()
        kind = 'state_copy' if value['role'] == 'candidate' else 'rollback_state'
        if value['operation_id'] != str(uuid5(UUID(value['command_id']), kind)): raise ValueError()
        profile = verify_profile_bytes(value['profile_json'].encode('utf-8'), value['profile_digest'])
        if profile.custodian_control is None or profile.generation_isolation is None: raise ValueError()
        paths = [getattr(profile.custodian_control, n) for n in ('listener_path', 'journal_path', 'journal_enrollment_path', 'copy_policy_path', 'owner_policy_path')]
        if path in paths: raise ValueError()
        result = CustodianOperation(path, pin, profile, value['profile_digest'], value['supervisor_public_key'],
            value['operation_id'], value['command_id'], value['run_id'], value['target_id'], value['fencing_generation'],
            value['manifest_digest'], value['source_backup_digest'], value['role'], value['timeout_ms'])
        result.check()
        return result
    except Exception:
        raise CustodianWorkerError() from None


def _policy_pins(operation):
    operation.check()
    control, isolation, daemon = (operation.profile.custodian_control, operation.profile.generation_isolation,
                                  operation.profile.daemon_policy)
    records = {}
    for name in ('journal_enrollment', 'copy_policy', 'owner_policy'):
        path = _path(getattr(control, name + '_path'))
        raw, _ = _root_file(path, 16384 if name == 'owner_policy' else 4096)
        if 'sha256:' + hashlib.sha256(raw).hexdigest() != getattr(control, name + '_digest'): raise ValueError()
        records[name] = _strict(raw, 16384 if name == 'owner_policy' else 4096)
    enrollment, copy, owner = records['journal_enrollment'], records['copy_policy'], records['owner_policy']
    if (type(enrollment) is not dict or set(enrollment) != {'schema', 'supervisor_public_key', 'manifest_digest',
            'source_backup_digest', 'provider_key_digest', 'provider_uid', 'provider_gid', 'journal_path'}
            or type(enrollment['schema']) is not int or enrollment['schema'] != 2
            or enrollment['supervisor_public_key'] != operation.supervisor_public_key
            or enrollment['manifest_digest'] != operation.manifest_digest or enrollment['source_backup_digest'] != operation.source_backup_digest
            or enrollment['provider_key_digest'] != 'sha256:' + hashlib.sha256(bytes.fromhex(isolation.provider_public_key)).hexdigest()
            or type(enrollment['provider_uid']) is not int or enrollment['provider_uid'] != isolation.provider_host_uid
            or type(enrollment['provider_gid']) is not int or enrollment['provider_gid'] != isolation.provider_host_gid
            or enrollment['journal_path'] != control.journal_path
            or type(copy) is not dict or set(copy) != {'schema', 'source', 'source_digest', 'staging_directory',
                    'maximum_bytes', 'maximum_total_bytes', 'minimum_free_bytes'}
            or type(copy['schema']) is not int or copy['schema'] != 1 or copy['source_digest'] != operation.source_backup_digest
            or type(copy['maximum_bytes']) is not int or not 0 < copy['maximum_bytes'] <= 512 * 1024 * 1024
            or type(copy['maximum_total_bytes']) is not int or not copy['maximum_bytes'] <= copy['maximum_total_bytes'] <= 16 * 1024**3
            or type(copy['minimum_free_bytes']) is not int or not 0 < copy['minimum_free_bytes'] <= 16 * 1024**3
            or type(owner) is not dict or set(owner) != {'schema', 'boot_id', 'daemon_pid', 'daemon_start_ticks', 'uid_map', 'gid_map', 'app_uid', 'app_gid'}
            or type(owner['schema']) is not int or owner['schema'] != 1
            or type(owner['daemon_pid']) is not int or owner['daemon_pid'] <= 0
            or type(owner['daemon_start_ticks']) is not int or owner['daemon_start_ticks'] <= 0
            or type(owner['boot_id']) is not str or str(UUID(owner['boot_id'])) != owner['boot_id']
            or type(owner['app_uid']) is not int or owner['app_uid'] != isolation.app_uid
            or type(owner['app_gid']) is not int or owner['app_gid'] != isolation.app_gid
            or _proc_map_rows(owner['uid_map']) != daemon.uid_map or _proc_map_rows(owner['gid_map']) != daemon.gid_map):
        raise ValueError()
    source, stage = _path(copy['source']), _path(copy['staging_directory'])
    if source.parent == stage or source == stage or stage in source.parents or source.parent in stage.parents: raise ValueError()
    operation.check()
    return owner


def preflight_custodian_operation(operation, supervisor, manifest):
    """Bind operation to actual supervisor enrollment; no listener or effects."""
    from .launch_supervisor import LaunchSupervisor, _boot_id
    from engineering_hub.container_pilot_manifest import PilotManifest
    try:
        if type(operation) is not CustodianOperation or type(supervisor) is not LaunchSupervisor or type(manifest) is not PilotManifest: raise ValueError()
        current = load_custodian_operation(operation.path)
        if current != operation: raise ValueError()
        owner = _policy_pins(current)
        if (supervisor.profile.digest() != current.profile_digest or supervisor.release.launch_profile_digest != current.profile_digest
                or supervisor.release.public_key.hex() != current.supervisor_public_key
                or manifest.digest() != current.manifest_digest or manifest.state_policy.baseline_backup_digest != current.source_backup_digest
                or supervisor._handoff_manifest_digest() != current.manifest_digest
                or type(owner['daemon_pid']) is not int or owner['daemon_pid'] != supervisor._daemon_pin.pid
                or type(owner['daemon_start_ticks']) is not int or owner['daemon_start_ticks'] != supervisor._daemon_pin.start_ticks
                or owner['boot_id'] != _boot_id()): raise ValueError()
        supervisor._check_daemon_maps(); current.check()
    except Exception:
        raise CustodianWorkerError() from None


def run_custodian_operation(path):
    """Run one root-approved private operation against the enrolled listener."""
    journal = channel = peer = parent = None
    try:
        started = time.monotonic()
        operation = load_custodian_operation(path); _policy_pins(operation)
        control, isolation = operation.profile.custodian_control, operation.profile.generation_isolation
        deadline = started + operation.timeout_ms / 1000
        if time.monotonic() >= deadline: raise ValueError()
        endpoint = _path(control.listener_path)
        parent = _root_parent(endpoint, private=False)
        parent_info = os.fstat(parent); named = os.stat(endpoint.name, dir_fd=parent, follow_symlinks=False)
        if (parent_info.st_uid != 0 or parent_info.st_gid != isolation.provider_host_gid
                or stat.S_IMODE(parent_info.st_mode) != 0o710 or not stat.S_ISSOCK(named.st_mode)
                or named.st_uid != 0 or named.st_gid != isolation.provider_host_gid or stat.S_IMODE(named.st_mode) != 0o660
                or (os.lstat(endpoint.parent).st_dev, os.lstat(endpoint.parent).st_ino) != (parent_info.st_dev, parent_info.st_ino)):
            raise ValueError()
        def check_socket():
            current_parent = os.fstat(parent)
            current_named = os.stat(endpoint.name, dir_fd=parent, follow_symlinks=False)
            path_parent = os.lstat(endpoint.parent)
            if ((current_parent.st_dev, current_parent.st_ino, current_parent.st_uid, current_parent.st_gid, stat.S_IMODE(current_parent.st_mode))
                    != (parent_info.st_dev, parent_info.st_ino, 0, isolation.provider_host_gid, 0o710)
                    or (path_parent.st_dev, path_parent.st_ino) != (parent_info.st_dev, parent_info.st_ino)
                    or not stat.S_ISSOCK(current_named.st_mode)
                    or (current_named.st_dev, current_named.st_ino, current_named.st_uid, current_named.st_gid, stat.S_IMODE(current_named.st_mode))
                       != (named.st_dev, named.st_ino, 0, isolation.provider_host_gid, 0o660)):
                raise ValueError()
        channel = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise ValueError()
        channel.settimeout(remaining)
        channel.connect(str(endpoint))
        check_socket()
        pid, uid, _ = struct.unpack('3i', channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != 0 or pid <= 0: raise ValueError()
        peer = _Process(pid); peer.check(); operation.check(); _policy_pins(operation)
        journal = CustodianAdmissionJournal(enrollment_path=_path(control.journal_enrollment_path),
                                            journal_path=_path(control.journal_path), create=False)
        if 'sha256:' + journal._enrollment_pin[1] != control.journal_enrollment_digest: raise ValueError()
        check_socket(); peer.check(); operation.check()
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise ValueError()
        expected = {name: getattr(operation, name) for name in ('command_id', 'run_id', 'target_id', 'fencing_generation',
                    'manifest_digest', 'source_backup_digest', 'role')}
        result = journal.execute_private_preparation(channel, operation_id=operation.operation_id,
            provider_uid=isolation.provider_host_uid, provider_gid=isolation.provider_host_gid,
            copy_policy_path=_path(control.copy_policy_path), owner_policy_path=_path(control.owner_policy_path),
            timeout_seconds=remaining, expected_context=expected, expected_copy_policy_digest=control.copy_policy_digest,
            expected_owner_policy_digest=control.owner_policy_digest, authority_anchor=(_path(operation.path), operation.identity),
            absolute_deadline=deadline)
        operation.check(); peer.check()
        if time.monotonic() >= deadline: raise ValueError()
        return result
    except Exception:
        raise CustodianWorkerError() from None
    finally:
        failed = False
        for resource in (journal, peer, channel):
            if resource is not None:
                try: resource.close()
                except Exception: failed = True
        if parent is not None:
            try: os.close(parent)
            except OSError: failed = True
        if failed: raise CustodianWorkerError() from None


class _Parser(argparse.ArgumentParser):
    def error(self, message): raise CustodianWorkerError()


def main(argv=None):
    try:
        parser = _Parser(allow_abbrev=False)
        parser.add_argument('--operation-file', required=True, action='append')
        options = parser.parse_args(argv)
        if len(options.operation_file) != 1: raise ValueError()
        run_custodian_operation(options.operation_file[0])
        print('private_prepared', flush=True)
        return 0
    except Exception:
        print('custodian_unavailable_preserve_evidence', file=sys.stderr, flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
