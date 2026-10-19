"""Fake CLI transcripts; actual daemon/isolation acceptance is separate."""
from copy import deepcopy
from dataclasses import replace
import json
import hashlib
import os
import sys
import time
from uuid import uuid4

import pytest

from engineering_hub.work_queue import WorkTarget, WorkWorker
from hub_validation_worker.core import ValidationEnrollment, ValidationJob, ValidationProfile
from hub_validation_worker.docker_backend import DockerCLIBackend, DockerImageSelection, DockerValidationError


CID = 'c' * 64
IMAGE = 'sha256:' + '1' * 64
ENV = ['PATH=/usr/local/bin:/usr/bin:/bin', 'LANG=C.UTF-8', 'PYTHON_VERSION=3.12.13']


@pytest.fixture
def rig(tmp_path, monkeypatch):
    enrollment = ValidationEnrollment('engineering-hub.validate', 'engineering-hub', 'staging-pc',
                                      'a' * 40, IMAGE, IMAGE, 'linux/amd64',
                                      ('/usr/local/bin/python', '-m', 'pytest', '-q'), ValidationProfile(), 60)
    job = ValidationJob(str(uuid4()), enrollment.action_key, enrollment.project, enrollment.machine_id,
                        'staging', WorkTarget(str(uuid4()), enrollment.source_commit, enrollment.artifact_digest),
                        WorkWorker('codex', str(uuid4())), 1)
    intent = str(uuid4())
    backend = DockerCLIBackend(sys.executable, 'isolated-test', 'pinned-daemon', config_directory=str(tmp_path))
    p = enrollment.profile
    config = dict(Image=IMAGE, User='10001:10001', Entrypoint=[enrollment.argv[0]],
                  Cmd=list(enrollment.argv[1:]), Env=ENV, Volumes=None, Healthcheck={'Test': ['NONE']},
                  Labels={'engineering-hub.validation.job': job.job_id,
                          'engineering-hub.validation.fence': '1', 'engineering-hub.validation.intent': intent})
    host = dict(ReadonlyRootfs=True, Privileged=False, NetworkMode='none', PortBindings={}, CapDrop=['ALL'],
                CapAdd=None, SecurityOpt=['no-new-privileges=true'], CgroupnsMode='private', IpcMode='private',
                PidMode='', UTSMode='', Memory=p.memory_bytes, MemorySwap=p.memory_bytes,
                NanoCpus=p.cpu_millicores * 1_000_000, PidsLimit=p.pids_limit, AutoRemove=False,
                LogConfig={'Type': 'none', 'Config': {}}, RestartPolicy={'Name': 'no', 'MaximumRetryCount': 0},
                Tmpfs={'/tmp': backend._tmpfs(p)}, Binds=None, Mounts=None, VolumesFrom=None, Devices=[],
                DeviceRequests=None, GroupAdd=None, Links=None, ExtraHosts=None)
    row = dict(Id=CID, Name='/' + backend._name(job), Image=IMAGE, Path=enrollment.argv[0],
               Args=list(enrollment.argv[1:]), Config=config, HostConfig=host, Mounts=[],
               State=dict(Status='created', Running=False, Paused=False, Restarting=False, Dead=False,
                          Error='', Pid=0, ExitCode=0, OOMKilled=False))
    transcript = []
    image = dict(Id=IMAGE, Os='linux', Architecture='amd64', Config=dict(Env=ENV, Cmd=['untrusted-default']))
    mutations = {}

    def run(args, deadline):
        backend._remaining(deadline)
        transcript.append(tuple(args))
        verb = tuple(args[:2])
        if args[0] == 'info':
            return json.dumps(mutations.get('info', dict(ID='pinned-daemon', OSType='linux', Architecture='x86_64')))
        if verb == ('image', 'inspect'):
            return json.dumps([image])
        if verb == ('container', 'create'):
            return mutations.get('create', CID + '\n')
        if verb == ('container', 'start'):
            if mutations.get('start_error'):
                raise RuntimeError('private daemon output must not escape')
            return CID + '\n'
        if verb == ('container', 'wait'):
            if mutations.get('wait_error'):
                raise TimeoutError('not proof of cancellation')
            return mutations.get('wait', '0\n')
        if verb == ('container', 'inspect'):
            result = deepcopy(row)
            if any(command[:2] == ('container', 'wait') for command in transcript):
                result['State']['Status'] = 'exited'
                for key, value in mutations.get('stopped', {}).items():
                    result['State'][key] = value
            return json.dumps([result])
        raise AssertionError('Unexpected CLI operation')

    monkeypatch.setattr(backend, '_run', run)
    return [backend, enrollment, job, intent, transcript, row, image, mutations]


def execute(rig):
    return rig[0].execute(rig[1], rig[2], rig[3], deadline_monotonic=time.monotonic() + 60)


def test_exact_fixed_profile_and_terminal_observation(rig):
    observation = execute(rig)
    assert observation.container_id == CID and observation.quiescent is True and observation.exit_code == 0
    assert observation.image_config_id == IMAGE and observation.profile_digest == rig[1].profile.digest()
    create = next(command for command in rig[4] if command[:2] == ('container', 'create'))
    assert create[-4:] == (IMAGE, '-m', 'pytest', '-q')
    for flag, expected in [('--pull', 'never'), ('--user', '10001:10001'), ('--network', 'none'),
                           ('--cap-drop', 'ALL'), ('--security-opt', 'no-new-privileges'),
                           ('--entrypoint', '/usr/local/bin/python'), ('--log-driver', 'none')]:
        assert create[create.index(flag) + 1] == expected
    assert '--read-only' in create and '--no-healthcheck' in create
    assert not any(value in create for value in ('--volume', '-v', '--mount', '--env', '-e', '--publish', '-p'))
    assert not any(command[0] in {'pull', 'logs', 'exec'} or 'rm' in command for command in rig[4])


@pytest.mark.parametrize('section,key,value', [
    ('HostConfig', 'Privileged', True), ('HostConfig', 'ReadonlyRootfs', False),
    ('HostConfig', 'NetworkMode', 'host'), ('HostConfig', 'Binds', ['/private:/host']),
    ('HostConfig', 'MemorySwap', -1), ('HostConfig', 'PidMode', 'host'),
    ('HostConfig', 'CapAdd', ['SYS_ADMIN']), ('HostConfig', 'AutoRemove', True),
    ('HostConfig', 'LogConfig', {'Type': 'json-file', 'Config': {}}),
    ('Config', 'User', '0:0'), ('Config', 'Env', ['SECRET=private']),
    ('Config', 'Entrypoint', ['/bin/sh']), ('Config', 'Labels', {}),
])
def test_actual_profile_mismatch_denies_before_start(rig, section, key, value):
    rig[5][section][key] = value
    with pytest.raises(DockerValidationError):
        execute(rig)
    assert not any(command[:2] == ('container', 'start') for command in rig[4])


@pytest.mark.parametrize('changes', [{'Running': True}, {'Pid': 2}, {'ExitCode': True},
                                     {'OOMKilled': True}, {'Status': 'running'}, {'ExitCode': 1}])
def test_terminal_readback_must_match_wait_and_quiescence(rig, changes):
    rig[7]['stopped'] = changes
    with pytest.raises(DockerValidationError):
        execute(rig)
    assert not any('rm' in command or 'stop' in command or 'kill' in command for command in rig[4])


@pytest.mark.parametrize('failure', ['start_error', 'wait_error'])
def test_uncertain_effect_no_retry_no_cleanup_no_error_output(rig, failure):
    rig[7][failure] = True
    with pytest.raises(DockerValidationError) as error:
        execute(rig)
    assert 'private' not in str(error.value) and 'cancellation' not in str(error.value)
    assert sum(command[:2] == ('container', 'create') for command in rig[4]) == 1
    assert sum(command[:2] == ('container', 'start') for command in rig[4]) == 1
    assert not any('rm' in command or 'stop' in command or 'kill' in command for command in rig[4])


@pytest.mark.parametrize('part', ['daemon', 'image', 'environment', 'volume', 'onbuild', 'healthcheck'])
def test_binding_and_unsafe_image_defaults_deny_before_create(rig, part):
    if part == 'daemon':
        rig[7]['info'] = dict(ID='different-daemon', OSType='linux', Architecture='x86_64')
    elif part == 'image':
        rig[6]['Id'] = 'sha256:' + '9' * 64
    else:
        name, value = {'environment': ('Env', ['TOKEN=private']), 'volume': ('Volumes', {'/data': {}}),
                       'onbuild': ('OnBuild', ['RUN anything']), 'healthcheck': ('Healthcheck', {'Test': ['CMD', 'anything']})}[part]
        rig[6]['Config'][name] = value
    with pytest.raises(DockerValidationError):
        execute(rig)
    assert not any(command[:2] == ('container', 'create') for command in rig[4])


def test_unenrolled_target_and_expired_deadline_never_start_cli(rig):
    backend, enrollment, job, intent, calls, *_ = rig
    wrong = replace(job, target=WorkTarget(job.target.task_id, 'd' * 40, job.target.artifact_digest))
    for target, deadline in [(wrong, time.monotonic() + 60), (job, time.monotonic() - 1)]:
        with pytest.raises(DockerValidationError):
            backend.execute(enrollment, target, intent, deadline_monotonic=deadline)
    assert calls == []


def test_real_subprocess_boundary_has_explicit_context_env_and_output_bound(tmp_path, monkeypatch):
    backend = DockerCLIBackend(sys.executable, 'pinned', 'daemon', config_directory=str(tmp_path))
    captured = []
    real = __import__('subprocess').Popen

    def launch(args, **kwargs):
        captured.append((args, kwargs))
        # Exercise the real bounded reader without Docker or network.
        return real([sys.executable, '-c', 'print("x" * 65537)'], **kwargs)

    monkeypatch.setenv('DOCKER_HOST', 'tcp://untrusted:2375')
    monkeypatch.setenv('SECRET_TOKEN', 'must-not-propagate')
    monkeypatch.setattr('hub_validation_worker.docker_backend.subprocess.Popen', launch)
    with pytest.raises(DockerValidationError):
        backend._run(('info',), time.monotonic() + 5)
    args, options = captured[0]
    assert args == [sys.executable, '--config', str(tmp_path), '--context', 'pinned', 'info']
    assert options['shell'] is False
    assert 'DOCKER_HOST' not in options['env'] and 'SECRET_TOKEN' not in options['env']


def test_constructor_is_explicit_and_inert(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('constructor must not invoke processes')
    monkeypatch.setattr('hub_validation_worker.docker_backend.subprocess.Popen', forbidden)
    DockerCLIBackend(sys.executable, 'pinned', 'daemon', config_directory=str(tmp_path))
    with pytest.raises(DockerValidationError):
        DockerCLIBackend('docker', 'pinned', 'daemon', config_directory=str(tmp_path))


def chain():
    manifest = dict(schemaVersion=2, mediaType='application/vnd.oci.image.manifest.v1+json',
                    config=dict(mediaType='application/vnd.oci.image.config.v1+json', digest=IMAGE, size=321),
                    layers=[])
    raw = json.dumps(manifest).encode()
    descriptor = dict(mediaType=manifest['mediaType'], digest='sha256:' + hashlib.sha256(raw).hexdigest(),
                      size=len(raw), platform=dict(os='linux', architecture='amd64'))
    index = dict(schemaVersion=2, mediaType='application/vnd.oci.image.index.v1+json', manifests=[descriptor])
    return index, manifest, raw


def indexed_rig(rig):
    index, _, raw = chain()
    selection = DockerImageSelection.from_oci(json.dumps(index).encode(), raw, platform='linux/amd64')
    rig[0]._selection = selection
    rig[1] = replace(rig[1], artifact_digest=selection.runnable_reference)
    rig[2] = replace(rig[2], target=replace(rig[2].target, artifact_digest=selection.runnable_reference))
    rig[6]['Id'] = selection.runnable_reference
    rig[6]['Descriptor'] = dict(mediaType='application/vnd.oci.image.index.v1+json',
                               digest=selection.runnable_reference, size=len(selection.index_bytes))
    rig[5]['Image'] = selection.runnable_reference
    rig[5]['Config']['Image'] = selection.runnable_reference
    rig[5]['ImageManifestDescriptor'] = deepcopy(index['manifests'][0])
    return selection


def test_containerd_index_selection_proves_config_chain_and_actual_manifest(rig):
    selection = indexed_rig(rig)
    observation = execute(rig)
    assert observation.image_config_id == IMAGE  # Never reinterpreted as index.
    create = next(command for command in rig[4] if command[:2] == ('container', 'create'))
    assert create[-4] == selection.runnable_reference
    assert IMAGE not in create


@pytest.mark.parametrize('part', ['missing_selected', 'wrong_selected', 'wrong_platform', 'wrong_root', 'wrong_config'])
def test_containerd_index_identity_mismatch_holds(rig, part):
    selection = indexed_rig(rig)
    if part == 'missing_selected':
        rig[5].pop('ImageManifestDescriptor')
    elif part == 'wrong_selected':
        rig[5]['ImageManifestDescriptor']['digest'] = 'sha256:' + 'f' * 64
    elif part == 'wrong_platform':
        rig[5]['ImageManifestDescriptor']['platform']['architecture'] = 'arm64'
    elif part == 'wrong_root':
        rig[6]['Descriptor']['digest'] = 'sha256:' + 'f' * 64
    else:
        bad_manifest = json.loads(selection.manifest_bytes)
        bad_manifest['config']['digest'] = 'sha256:' + 'f' * 64
        bad_raw = json.dumps(bad_manifest).encode()
        index = json.loads(selection.index_bytes)
        index['manifests'][0].update(digest='sha256:' + hashlib.sha256(bad_raw).hexdigest(), size=len(bad_raw))
        rig[0]._selection = DockerImageSelection.from_oci(json.dumps(index).encode(), bad_raw, platform='linux/amd64')
    with pytest.raises(DockerValidationError):
        execute(rig)
    assert not any(command[:2] == ('container', 'start') for command in rig[4])


@pytest.mark.parametrize('case', ['tamper', 'duplicate_platform', 'size', 'config_type', 'oversize', 'urls', 'duplicate_key'])
def test_original_oci_chain_is_strict_bounded_and_linked(case):
    index, manifest, raw = chain()
    if case == 'tamper':
        raw += b' '
    elif case == 'duplicate_platform':
        index['manifests'] *= 2
    elif case == 'size':
        index['manifests'][0]['size'] += 1
    elif case == 'config_type':
        manifest['config']['mediaType'] = 'untrusted'
        raw = json.dumps(manifest).encode()
        index['manifests'][0].update(digest='sha256:' + hashlib.sha256(raw).hexdigest(), size=len(raw))
    elif case == 'urls':
        index['manifests'][0]['urls'] = ['https://untrusted.invalid']
    index_bytes = json.dumps(index).encode()
    if case == 'oversize':
        index_bytes = b' ' * 65537
    elif case == 'duplicate_key':
        index_bytes = b'{"schemaVersion":2,' + index_bytes[1:]
    with pytest.raises(DockerValidationError):
        DockerImageSelection.from_oci(index_bytes, raw, platform='linux/amd64')


@pytest.mark.parametrize('value,accepted', [('HF_HUB_OFFLINE=1', True), ('HF_HUB_OFFLINE=0', False),
                                         ('UV_PROJECT_ENVIRONMENT=/opt/validation-venv', True),
                                         ('UV_PROJECT_ENVIRONMENT=/private', False), ('PYTHONPATH=/app/src', False)])
def test_additional_public_image_environment_values_are_exact(rig, value, accepted):
    rig[6]['Config']['Env'] = [*ENV, value]
    rig[5]['Config']['Env'] = [*ENV, value]
    if accepted:
        assert execute(rig).quiescent is True
    else:
        with pytest.raises(DockerValidationError):
            execute(rig)
