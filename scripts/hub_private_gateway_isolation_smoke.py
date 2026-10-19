#!/usr/bin/env python3
"""Offline, disposable two-container gateway isolation smoke test.

Requires an already-local validation image by immutable Docker image ID. Only
synthetic fixture credentials/effects are used. This does not deploy Engineering
Hub, touch an existing container, prove a real Docker adapter, or test reboot.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import uuid

_IMAGE = re.compile(r'sha256:[0-9a-f]{64}\Z')
_ID = re.compile(r'[0-9a-f]{64}\Z')
_LABEL = 'com.ledgetop.engineering-hub.isolation-smoke'
_FIXTURE = '/validation/tests/_hub_gateway_isolation_fixture.py'
_TMP_OPTIONS = 'rw,noexec,nosuid,nodev,size=64m,mode=1777'
_PRIVATE_OPTIONS = 'rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700'
_ENV = {'PATH', 'LANG', 'GPG_KEY', 'PYTHON_VERSION', 'PYTHON_SHA256',
        'PYTHONDONTWRITEBYTECODE', 'PYTHONUNBUFFERED', 'HF_HUB_OFFLINE',
        'UV_PROJECT_ENVIRONMENT'}


class SmokeError(RuntimeError):
    """Sanitized failure; never echoes commands, credentials, or container logs."""


def run(args, *, input_text=None, timeout=30):
    try:
        result = subprocess.run(args, input=input_text, capture_output=True,
                                text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        raise SmokeError('bounded local Docker operation failed') from None
    if result.returncode != 0 or len(result.stdout) > 1024 * 1024:
        raise SmokeError('local Docker operation failed')
    return result.stdout.strip()


def _json(raw):
    try:
        result = json.loads(raw)
    except (ValueError, TypeError):
        raise SmokeError('unexpected local validation response') from None
    if type(result) is not dict:
        raise SmokeError('unexpected local validation response')
    return result


class _Docker:
    def __init__(self):
        self.context = run(['docker', 'context', 'show'])
        if not self.context or '\n' in self.context or len(self.context) > 200:
            raise SmokeError('local Docker context is required')
        raw = run(['docker', 'context', 'inspect', self.context,
                   '--format', '{{json .Endpoints.docker.Host}}'])
        try:
            endpoint = json.loads(raw)
        except (TypeError, ValueError):
            raise SmokeError('local Docker endpoint is required') from None
        if (type(endpoint) is not str or not (
                endpoint.startswith('unix:///')
                or endpoint.startswith('npipe:////./pipe/'))):
            raise SmokeError('remote Docker endpoints are not allowed')
        if self('info', '--format', '{{.OSType}}') != 'linux':
            raise SmokeError('local Linux Docker engine is required')

    def __call__(self, *args, input_text=None, timeout=30):
        return run(['docker', '--context', self.context, *args],
                   input_text=input_text, timeout=timeout)


def _owned_container(docker, name, expected_id, owner):
    value = _json(docker('container', 'inspect', name, '--format', '{{json .}}'))
    actual = value.get('Id')
    config = value.get('Config')
    labels = config.get('Labels') if type(config) is dict else None
    if (type(actual) is not str or _ID.fullmatch(actual) is None
            or (expected_id is not None and actual != expected_id)
            or value.get('Name') != '/' + name
            or type(labels) is not dict or labels.get(_LABEL) != owner):
        raise SmokeError('container ownership could not be verified; preserved')
    return value


def _owned_volume(docker, name, expected, owner):
    value = _json(docker('volume', 'inspect', name, '--format', '{{json .}}'))
    labels = value.get('Labels')
    if (value.get('Name') != name or value.get('Driver') != 'local'
            or value.get('Scope') != 'local' or value.get('Options') not in (None, {})
            or type(labels) is not dict or labels.get(_LABEL) != owner
            or type(value.get('CreatedAt')) is not str
            or (expected is not None and any(value.get(key) != expected.get(key)
                for key in ('Name', 'CreatedAt', 'Driver', 'Scope', 'Options', 'Labels')))):
        raise SmokeError('volume ownership could not be verified; preserved')
    return value


def _profile(value, volume, *, initializer=False, private=False):
    host = value.get('HostConfig', {})
    config = value.get('Config', {})
    mounts = value.get('Mounts', [])
    if (type(host) is not dict or type(config) is not dict or type(mounts) is not list
            or any(type(mount) is not dict for mount in mounts)):
        raise SmokeError('invalid disposable container isolation profile')
    volumes = [mount for mount in mounts if mount.get('Type') == 'volume']
    tmpfs = host.get('Tmpfs') or {}
    env = config.get('Env') or []
    expected_tmpfs = {'/tmp': _TMP_OPTIONS}
    if private:
        expected_tmpfs['/private'] = _PRIVATE_OPTIONS
    if (host.get('NetworkMode') != 'none' or host.get('ReadonlyRootfs') is not True
            or host.get('Privileged') is not False or host.get('PidMode') not in ('', None)
            or host.get('IpcMode') != 'private' or host.get('CgroupnsMode') != 'private'
            or host.get('Binds') not in (None, [])
            or host.get('Devices') not in (None, [])
            or host.get('DeviceRequests') not in (None, [])
            or host.get('PortBindings') not in (None, {})
            or set(host.get('CapDrop') or []) != {'ALL'}
            or set(host.get('CapAdd') or []) not in (
                ({'CHOWN'}, {'CAP_CHOWN'}) if initializer else (set(),))
            or 'no-new-privileges:true' not in (host.get('SecurityOpt') or [])
            or config.get('User') != ('0:0' if initializer else '10001:10001')
            or len(volumes) != 1 or volumes[0].get('Name') != volume
            or volumes[0].get('Destination') != '/socket'
            or volumes[0].get('RW') is not (initializer or private)
            or any(mount.get('Type') not in {'volume', 'tmpfs'} for mount in mounts)
            or tmpfs != expected_tmpfs
            or any(type(item) is not str or '=' not in item
                   or item.split('=', 1)[0] not in _ENV for item in env)
            or type(host.get('Memory')) is not int or host['Memory'] != 1024**3
            or type(host.get('MemorySwap')) is not int or host['MemorySwap'] != 1024**3
            or type(host.get('NanoCpus')) is not int or host['NanoCpus'] != 2_000_000_000
            or type(host.get('PidsLimit')) is not int or host['PidsLimit'] != 128):
        raise SmokeError('disposable container isolation profile did not match')


def _exec(docker, name, *args, input_value=None, timeout=30):
    command = ['container', 'exec']
    if input_value is not None:
        command.append('-i')
    command.extend([name, 'python', _FIXTURE, *args])
    return _json(docker(*command, input_text=(json.dumps(input_value)
                 if input_value is not None else None), timeout=timeout))


def _ready(docker, name, role):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            result = _exec(docker, name, 'ready', '--role', role, timeout=5)
        except SmokeError:
            state = _json(docker('container', 'inspect', name,
                                '--format', '{{json .State}}', timeout=5))
            if state.get('Running') is not True:
                raise SmokeError('disposable fixture exited before readiness') from None
            time.sleep(0.1)
            continue
        if (result.get('ready') is not True or type(result.get('port')) is not int
                or not 1 <= result['port'] <= 65535):
            raise SmokeError('invalid disposable fixture readiness')
        return result
    raise SmokeError('disposable fixture readiness timed out')


def _cleanup(docker, containers, volume, expected_volume, owner):
    # If any container cannot be verified/removed, do not touch its volume.
    unknown = False
    for name, expected_id in reversed(list(containers.items())):
        try:
            value = _owned_container(docker, name, expected_id, owner)
            docker('container', 'rm', '--force', value['Id'], timeout=30)
        except SmokeError:
            unknown = True
    if unknown:
        raise SmokeError('disposable cleanup incomplete; owned volume preserved')
    _owned_volume(docker, volume, expected_volume, owner)
    docker('volume', 'rm', volume, timeout=30)


def _case(docker, image, case):
    owner = uuid.uuid4().hex
    volume = 'eh-isolation-' + owner
    containers = {}
    volume_created = False
    expected_volume = None
    try:
        docker('volume', 'create', '--label', _LABEL + '=' + owner, volume)
        volume_created = True
        expected_volume = _owned_volume(docker, volume, None, owner)
        base = ['--pull=never', '--label', _LABEL + '=' + owner,
                '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges:true', '--ipc', 'private',
                '--cgroupns', 'private', '--cpus', '2', '--memory', '1g', '--memory-swap', '1g',
                '--pids-limit', '128', '--workdir', '/validation',
                '--entrypoint', 'python',
                '--tmpfs', '/tmp:' + _TMP_OPTIONS,
                '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'PYTHONUNBUFFERED=1',
                '--env', 'HF_HUB_OFFLINE=1']
        initializer = 'eh-isolation-init-' + owner
        mount = 'type=volume,src=' + volume + ',dst=/socket,volume-nocopy'
        containers[initializer] = None
        init_script = (
            'import os; assert os.listdir("/socket")==[]; '
            'os.mkdir("/socket/private",0o700); '
            'os.chown("/socket/private",10001,10001)')
        identifier = docker('container', 'create', '--name', initializer, *base,
                            '--mount', mount, '--user', '0:0', '--cap-add', 'CHOWN',
                            image, '-c', init_script)
        containers[initializer] = identifier
        value = _owned_container(docker, initializer, identifier, owner)
        _profile(value, volume, initializer=True)
        docker('container', 'start', '--attach', initializer)
        state = _json(docker('container', 'inspect', initializer, '--format', '{{json .State}}'))
        if state.get('ExitCode') != 0 or state.get('Running') is not False:
            raise SmokeError('disposable socket volume initialization failed')
        docker('container', 'rm', identifier)
        del containers[initializer]
        names = {}
        readiness = {}
        for role in ('private', 'public'):
            name = 'eh-isolation-' + role + '-' + owner
            containers[name] = None
            extra = (['--tmpfs', '/private:' + _PRIVATE_OPTIONS]
                     if role == 'private' else [])
            command = (['-m', 'engineering_hub.public_browser', '--socket',
                        '/socket/private/browser.sock', '--port', '8771']
                       if role == 'public' else [_FIXTURE, role, '--socket',
                            '/socket/private/browser.sock', '--case', case])
            identifier = docker('container', 'create', '--name', name, *base,
                '--mount', mount + (',readonly' if role == 'public' else ''),
                '--user', '10001:10001', *extra, image, *command)
            containers[name] = identifier
            value = _owned_container(docker, name, identifier, owner)
            _profile(value, volume, private=role == 'private')
            if role == 'public' and '/private' in (value['HostConfig'].get('Tmpfs') or {}):
                raise SmokeError('public container received a private state mount')
            docker('container', 'start', identifier)
            names[role] = name
            # The public client checks its UDS immediately; the private listener
            # must actually be bound before the public process is constructed.
            readiness[role] = _ready(docker, name, role)
        private = readiness['private']
        public = readiness['public']
        probe = _exec(docker, names['public'], 'public-probe')
        if (probe.get('private_paths_absent') is not True or probe.get('no_agent_env') is not True
                or probe.get('private_role_visible') is not False
                or probe.get('socket_volume_only_endpoint') is not True
                or probe.get('public_socket_mount_readonly') is not True
                or probe.get('public_loopback_only') is not True
                or private.get('private_paths_present') is not True
                or type(private.get('pid_namespace')) is not str
                or type(private.get('mount_namespace')) is not str
                or public.get('pid_namespace') != probe.get('pid_namespace')
                or public.get('mount_namespace') != probe.get('mount_namespace')
                or private['pid_namespace'] == public.get('pid_namespace')
                or private['mount_namespace'] == public.get('mount_namespace')):
            raise SmokeError('private state or namespace isolation was not proven')
        assertion = _exec(docker, names['private'], 'assertion')
        if type(assertion.get('assertion')) is not str or not assertion['assertion']:
            raise SmokeError('synthetic browser assertion unavailable')
        task = _exec(docker, names['public'], 'browser-start', '--port', str(public['port']),
                     '--case', case, input_value=assertion)
        del assertion
        task_id = task.get('task_id')
        if type(task_id) is not str or not task_id or len(task_id) > 100:
            raise SmokeError('synthetic browser task was not created')
        job = _exec(docker, names['private'], 'agents', '--port', str(private['port']),
                    '--task', task_id, '--case', case, timeout=60)
        job_id = job.get('job_id')
        expected_state = 'succeeded' if case == 'healthy' else 'failed'
        if type(job_id) is not str or not job_id or job.get('state') != expected_state:
            raise SmokeError('synthetic staging work did not reach its expected terminal state')
        final = _exec(docker, names['public'], 'browser-finish', '--port', str(public['port']),
                      '--job', job_id, '--case', case)
        if final.get('verified') is not True or final.get('state') != expected_state:
            raise SmokeError('browser did not verify synthetic staging result')
        return {'case': case, 'verified': True, 'state': expected_state,
                'namespace_isolated': True, 'private_paths_absent': True}
    finally:
        if volume_created:
            _cleanup(docker, containers, volume, expected_volume, owner)


def smoke(image):
    if type(image) is not str or _IMAGE.fullmatch(image) is None:
        raise ValueError('an immutable already-local sha256 Docker image ID is required')
    docker = _Docker()
    inspected = _json(docker('image', 'inspect', image, '--format', '{{json .}}'))
    env = inspected.get('Config', {}).get('Env') or []
    if (inspected.get('Id') != image or inspected.get('Os') != 'linux'
            or any(type(item) is not str or '=' not in item
                   or item.split('=', 1)[0] not in _ENV for item in env)):
        raise SmokeError('local immutable validation image did not match the safe profile')
    return {'schema': 'hub-private-gateway-isolation-v1', 'image': image,
            'cases': [_case(docker, image, case) for case in ('healthy', 'rollback')],
            'scope': 'disposable-container-synthetic-only'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    try:
        result = smoke(args.image)
    except (SmokeError, ValueError) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
