"""Explicit local-image Docker CLI backend; no listeners, discovery or retries.

Enrollment is a trusted operator input, not proof derived from an agent, image
label or receipt. The core must durably reserve intent BEFORE calling execute.
No resource is removed here, including after uncertainty. CLI timeout kills only
the CLI process and NEVER proves that an Engine effect or container stopped.
"""
from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from uuid import UUID


class DockerValidationError(RuntimeError):
    """Sanitized uncertain/denied validation; never includes daemon output."""


_ID = re.compile(r'[a-f0-9]{64}\Z')
_PUBLIC_ENV = frozenset({'PATH', 'LANG', 'LC_ALL', 'TZ', 'PYTHONUNBUFFERED',
                         'PYTHONDONTWRITEBYTECODE', 'PYTHON_VERSION', 'PYTHON_SHA256', 'GPG_KEY'})
_FIXED_PUBLIC_ENV = {'HF_HUB_OFFLINE': '1', 'UV_PROJECT_ENVIRONMENT': '/opt/validation-venv'}
_LABEL = 'engineering-hub.validation.'
_OUTPUT_LIMIT = 65536
_DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')
_INDEX_TYPE = 'application/vnd.oci.image.index.v1+json'
_MANIFEST_TYPE = 'application/vnd.oci.image.manifest.v1+json'
_CONFIG_TYPE = 'application/vnd.oci.image.config.v1+json'


def _require(condition):
    if not condition:
        raise DockerValidationError('Validation Engine evidence denied')


def _object(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))


def _platform(value, expected):
    return type(value) is dict and value == dict(zip(('os', 'architecture'), expected.split('/')))


def _descriptor(value):
    _require(type(value) is dict and {'mediaType', 'digest', 'size'} <= set(value)
             and set(value) <= {'mediaType', 'digest', 'size', 'platform', 'annotations', 'artifactType'}
             and type(value['digest']) is str and _DIGEST.fullmatch(value['digest']) is not None
             and type(value['size']) is int and 0 < value['size'] <= 16 * 1024**3
             and type(value['mediaType']) is str)


@dataclass(frozen=True)
class DockerImageSelection:
    """Immutable original OCI chain, supplied by independent local enrollment.

    Construct through from_oci with original bounded bytes, never reserialized
    JSON, image labels or agent request fields. This establishes content links,
    not a CI issuer/provenance or complete layer authenticity policy.
    """
    index_bytes: bytes
    manifest_bytes: bytes
    platform: str

    def __post_init__(self):
        try:
            _require(type(self.index_bytes) is bytes and 1 <= len(self.index_bytes) <= _OUTPUT_LIMIT
                     and type(self.manifest_bytes) is bytes and 1 <= len(self.manifest_bytes) <= _OUTPUT_LIMIT
                     and type(self.platform) is str and self.platform in {'linux/amd64', 'linux/arm64'})
            index, manifest = _object(self.index_bytes), _object(self.manifest_bytes)
            _require(type(index) is dict and {'schemaVersion', 'mediaType', 'manifests'} <= set(index)
                     and set(index) <= {'schemaVersion', 'mediaType', 'manifests', 'annotations'}
                     and type(index['schemaVersion']) is int and index['schemaVersion'] == 2
                     and index['mediaType'] == _INDEX_TYPE
                     and type(index['manifests']) is list and 1 <= len(index['manifests']) <= 32)
            selected = []
            for descriptor in index['manifests']:
                _descriptor(descriptor)
                _require(descriptor['mediaType'] == _MANIFEST_TYPE)
                if _platform(descriptor.get('platform'), self.platform):
                    selected.append(descriptor)
            _require(len(selected) == 1 and selected[0]['digest'] == self.platform_manifest_digest
                     and selected[0]['size'] == len(self.manifest_bytes))
            _require(type(manifest) is dict and {'schemaVersion', 'mediaType', 'config', 'layers'} <= set(manifest)
                     and set(manifest) <= {'schemaVersion', 'mediaType', 'config', 'layers', 'annotations'}
                     and type(manifest['schemaVersion']) is int and manifest['schemaVersion'] == 2
                     and manifest['mediaType'] == _MANIFEST_TYPE)
            _descriptor(manifest['config'])
            _require(manifest['config']['mediaType'] == _CONFIG_TYPE and manifest['config']['size'] <= 1024**2
                     and type(manifest['layers']) is list and len(manifest['layers']) <= 256)
            for layer in manifest['layers']:
                _descriptor(layer)
                _require(layer['mediaType'] in {'application/vnd.oci.image.layer.v1.tar',
                         'application/vnd.oci.image.layer.v1.tar+gzip', 'application/vnd.oci.image.layer.v1.tar+zstd'})
        except Exception:
            raise DockerValidationError('Validation image content chain unavailable') from None

    @classmethod
    def from_oci(cls, index_bytes, manifest_bytes, *, platform):
        return cls(index_bytes, manifest_bytes, platform)

    @property
    def runnable_reference(self):
        return 'sha256:' + hashlib.sha256(self.index_bytes).hexdigest()

    @property
    def platform_manifest_digest(self):
        return 'sha256:' + hashlib.sha256(self.manifest_bytes).hexdigest()

    @property
    def image_config_id(self):
        return _object(self.manifest_bytes)['config']['digest']


class DockerCLIBackend:
    """A single operator-specified CLI/config/context and pinned daemon identity.

    Constructor only validates strings; no subprocess, network or file writes.
    Context configuration and the executable are trusted operator-controlled
    inputs. The image config ID is independently enrolled by the operator;
    this backend does not create provenance by trusting image labels.
    """

    def __init__(self, executable, context, expected_daemon_id, *, config_directory, image_selection=None):
        if (type(executable) is not str or not Path(executable).is_absolute()
                or type(config_directory) is not str or not Path(config_directory).is_absolute()
                or any(c in executable + config_directory for c in '\x00\r\n')
                or type(context) is not str or re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}', context) is None
                or type(expected_daemon_id) is not str or not 1 <= len(expected_daemon_id) <= 128
                or re.fullmatch(r'[a-zA-Z0-9:_.-]+', expected_daemon_id) is None):
            raise DockerValidationError('Explicit validation Engine binding required')
        if image_selection is not None and type(image_selection) is not DockerImageSelection:
            raise DockerValidationError('Explicit validation image selection required')
        self._selection = image_selection
        self._prefix = (executable, '--config', config_directory, '--context', context)
        self._daemon = expected_daemon_id
        # No ambient Docker host, context, token, proxy or TLS environment.
        self._environment = {'PATH': os.defpath, 'HOME': config_directory}
        for key in ('SYSTEMROOT', 'WINDIR'):
            if os.environ.get(key):
                self._environment[key] = os.environ[key]

    @staticmethod
    def _remaining(deadline):
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise DockerValidationError('Validation deadline unavailable')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DockerValidationError('Validation deadline exhausted')
        return remaining

    def _run(self, arguments, deadline):
        """Bound output while waiting; killing CLI does not cancel Engine work."""
        remaining = self._remaining(deadline)
        process = None
        data, errors = [], []
        reader = None
        try:
            process = subprocess.Popen([*self._prefix, *arguments], shell=False,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, env=self._environment)

            def read():
                try:
                    data.append(process.stdout.read(_OUTPUT_LIMIT + 1))
                    if len(data[0]) > _OUTPUT_LIMIT:
                        process.kill()
                except Exception:
                    errors.append(True)

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            process.wait(timeout=remaining)
            reader.join(timeout=self._remaining(deadline))
            _require(not reader.is_alive() and not errors and len(data) == 1
                     and len(data[0]) <= _OUTPUT_LIMIT and process.returncode == 0)
            self._remaining(deadline)
            return data[0].decode('utf-8')
        except Exception:
            raise DockerValidationError('Validation Engine operation unavailable') from None
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                if reader is not None:
                    reader.join(timeout=2)
                if process.stdout is not None and (reader is None or not reader.is_alive()):
                    process.stdout.close()

    def _runner(self, enrollment, deadline):
        info = _object(self._run(('info', '--format', '{{json .}}'), deadline))
        _require(type(info) is dict and info.get('ID') == self._daemon
                 and info.get('OSType') == 'linux')
        expected_arch = enrollment.platform.split('/')[1]
        _require(info.get('Architecture') in ({'amd64', 'x86_64'} if expected_arch == 'amd64' else {'arm64', 'aarch64'}))

    def _image(self, enrollment, deadline):
        reference = self._reference(enrollment)
        data = _object(self._run(('image', 'inspect', reference), deadline))
        _require(type(data) is list and len(data) == 1 and type(data[0]) is dict)
        image = data[0]
        _require(image.get('Id') == reference
                 and image.get('Os') == 'linux' and image.get('Architecture') == enrollment.platform.split('/')[1])
        if self._selection is not None:
            self._selected_descriptor(image.get('Descriptor'), root=True)
        config = image.get('Config')
        _require(type(config) is dict and not config.get('Volumes') and not config.get('OnBuild')
                 and not config.get('Healthcheck') and not config.get('ExposedPorts'))
        if len(enrollment.argv) == 1:
            _require(not config.get('Cmd'))  # Docker otherwise preserves an image Cmd.
        environment = config.get('Env') or []
        _require(type(environment) is list and len(environment) <= 32)
        names = set()
        for value in environment:
            _require(type(value) is str and len(value) <= 4096 and '=' in value
                     and all(32 <= ord(c) <= 126 for c in value))
            name, public_value = value.split('=', 1)
            _require((name in _PUBLIC_ENV or _FIXED_PUBLIC_ENV.get(name) == public_value)
                     and name not in names)
            names.add(name)
        return environment

    def _reference(self, enrollment):
        if self._selection is None:
            _require(enrollment.artifact_digest == enrollment.image_config_id)
            return enrollment.image_config_id
        _require(self._selection.image_config_id == enrollment.image_config_id
                 and self._selection.platform == enrollment.platform
                 and enrollment.artifact_digest == self._selection.runnable_reference)
        return self._selection.runnable_reference

    def _selected_descriptor(self, descriptor, *, root=False):
        _descriptor(descriptor)
        selection = self._selection
        if root:
            _require(descriptor['digest'] == selection.runnable_reference
                     and descriptor['mediaType'] == _INDEX_TYPE and descriptor['size'] == len(selection.index_bytes))
        else:
            _require(descriptor['digest'] == selection.platform_manifest_digest
                     and descriptor['mediaType'] == _MANIFEST_TYPE
                     and descriptor['size'] == len(selection.manifest_bytes)
                     and _platform(descriptor.get('platform'), selection.platform))

    def _inspect(self, enrollment, job, intent, container_id, environment, *, stopped, deadline):
        data = _object(self._run(('container', 'inspect', container_id), deadline))
        _require(type(data) is list and len(data) == 1 and type(data[0]) is dict)
        row = data[0]
        config, host, state = row.get('Config'), row.get('HostConfig'), row.get('State')
        _require(all(type(v) is dict for v in (config, host, state)))
        profile = enrollment.profile
        name = self._name(job)
        labels = {_LABEL + 'job': job.job_id, _LABEL + 'fence': str(job.fence), _LABEL + 'intent': intent}
        _require(row.get('Id') == container_id and row.get('Name') == '/' + name
                 and row.get('Image') == self._reference(enrollment)
                 and row.get('Path') == enrollment.argv[0] and row.get('Args') == list(enrollment.argv[1:])
                 and config.get('Image') == self._reference(enrollment)
                 and config.get('User') == f'{profile.uid}:{profile.gid}'
                 and config.get('Entrypoint') == [enrollment.argv[0]]
                 and (config.get('Cmd') or []) == list(enrollment.argv[1:])
                 and (config.get('Env') or []) == environment
                 and all(config.get('Labels', {}).get(k) == v for k, v in labels.items())
                 and not config.get('Volumes') and config.get('Healthcheck') == {'Test': ['NONE']})
        if self._selection is not None:
            self._selected_descriptor(row.get('ImageManifestDescriptor'))
        _require(host.get('ReadonlyRootfs') is True and host.get('Privileged') is False
                 and host.get('NetworkMode') == 'none' and not host.get('PortBindings')
                 and host.get('CapDrop') == ['ALL'] and not host.get('CapAdd')
                 and host.get('SecurityOpt') in (['no-new-privileges'], ['no-new-privileges=true'])
                 and host.get('CgroupnsMode') == 'private' and host.get('IpcMode') == 'private'
                 and host.get('PidMode', '') == '' and not host.get('UTSMode')
                 and host.get('Memory') == profile.memory_bytes
                 and host.get('MemorySwap') == profile.memory_bytes
                 and host.get('NanoCpus') == profile.cpu_millicores * 1_000_000
                 and host.get('PidsLimit') == profile.pids_limit
                 and host.get('AutoRemove') is False
                 and host.get('LogConfig') == {'Type': 'none', 'Config': {}}
                 and host.get('RestartPolicy') == {'Name': 'no', 'MaximumRetryCount': 0}
                 and host.get('Tmpfs') == {'/tmp': self._tmpfs(profile)}
                 and not any(host.get(k) for k in ('Binds', 'Mounts', 'VolumesFrom', 'Devices',
                                                   'DeviceRequests', 'GroupAdd', 'Links', 'ExtraHosts')))
        mounts = row.get('Mounts')
        _require(type(mounts) is list and all(type(m) is dict and m.get('Type') == 'tmpfs'
                 and m.get('Destination') == '/tmp' and m.get('RW') is True for m in mounts)
                 and len(mounts) <= 1)
        _require(state.get('Paused') is False and state.get('Restarting') is False
                 and state.get('Dead') is False and state.get('Error') == '')
        if stopped:
            _require(state.get('Status') == 'exited' and state.get('Running') is False
                     and type(state.get('Pid')) is int and state['Pid'] == 0
                     and type(state.get('ExitCode')) is int and 0 <= state['ExitCode'] <= 255
                     and state.get('OOMKilled') is False)
        else:
            _require(state.get('Status') == 'created' and state.get('Running') is False)
        return state

    @staticmethod
    def _name(job):
        return 'hub-validation-' + job.job_id + '-' + str(job.fence)

    @staticmethod
    def _tmpfs(profile):
        return f'rw,noexec,nosuid,nodev,size={profile.tmpfs_bytes},mode=1777'

    def execute(self, enrollment, job, intent_id, *, deadline_monotonic):
        # Import only at explicit invocation; construction/import never starts a
        # process or discovers daemon credentials/configuration.
        from .core import ValidationEnrollment, ValidationJob, ValidationObservation
        try:
            _require(type(enrollment) is ValidationEnrollment and type(job) is ValidationJob
                     and type(intent_id) is str and str(UUID(intent_id)) == intent_id
                     and job.environment == 'staging' and job.action_key == enrollment.action_key
                     and job.project == enrollment.project and job.machine_id == enrollment.machine_id
                     and enrollment.profile.uid == 10001 and enrollment.profile.gid == 10001
                     and job.target.source_commit == enrollment.source_commit
                     and job.target.artifact_digest == enrollment.artifact_digest)
            deadline = min(deadline_monotonic, time.monotonic() + enrollment.timeout_seconds)
            self._remaining(deadline)
            self._runner(enrollment, deadline)
            environment = self._image(enrollment, deadline)
            self._runner(enrollment, deadline)
            p = enrollment.profile
            args = ('container', 'create', '--pull', 'never', '--name', self._name(job),
                    '--label', _LABEL + 'job=' + job.job_id, '--label', _LABEL + 'fence=' + str(job.fence),
                    '--label', _LABEL + 'intent=' + intent_id, '--user', f'{p.uid}:{p.gid}',
                    '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                    '--security-opt', 'no-new-privileges', '--cgroupns', 'private', '--ipc', 'private',
                    '--memory', str(p.memory_bytes), '--memory-swap', str(p.memory_bytes),
                    '--cpus', str(p.cpu_millicores / 1000), '--pids-limit', str(p.pids_limit),
                    '--tmpfs', '/tmp:' + self._tmpfs(p), '--restart', 'no', '--no-healthcheck', '--log-driver', 'none',
                    '--entrypoint', enrollment.argv[0], self._reference(enrollment), *enrollment.argv[1:])
            container_id = self._run(args, deadline).strip()
            _require(_ID.fullmatch(container_id) is not None)
            self._runner(enrollment, deadline)
            self._inspect(enrollment, job, intent_id, container_id, environment, stopped=False, deadline=deadline)
            self._runner(enrollment, deadline)
            _require(self._run(('container', 'start', container_id), deadline).strip() == container_id)
            self._runner(enrollment, deadline)
            waited = self._run(('container', 'wait', container_id), deadline).strip()
            _require(re.fullmatch(r'0|[1-9][0-9]{0,2}', waited) is not None and int(waited) <= 255)
            self._runner(enrollment, deadline)
            state = self._inspect(enrollment, job, intent_id, container_id, environment, stopped=True, deadline=deadline)
            _require(state['ExitCode'] == int(waited))
            self._runner(enrollment, deadline)
            return ValidationObservation(container_id, enrollment.image_config_id,
                                         enrollment.profile.digest(), state['ExitCode'], True)
        except Exception:
            raise DockerValidationError('Validation outcome held; inspect original operation') from None
