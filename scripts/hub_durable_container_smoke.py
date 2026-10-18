"""Offline disposable app-image restart and SQLite backup/restore proof.

No production data, credentials, host mounts, Docker socket or exposed ports.
This is NOT autonomous host rollout, OAuth, TrueNAS, or machine reboot proof.
All uniquely owned test containers and volumes are removed after verification.
"""
import argparse
import json
import re
import subprocess
import uuid


LABEL = 'engineering-hub.durable-smoke-owner'
PROBE = r'''
import json, os, re, stat, sys, time, urllib.error, urllib.request
from pathlib import Path
mode, commit = sys.argv[1:3]
assert os.getuid() == os.getgid() == 10001
assert stat.S_IMODE(Path('/var/lib/hub').stat().st_mode) == 0o700
base = 'http://127.0.0.1:8770'
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline = time.monotonic() + 20
def request(path, token=None, payload=None):
    headers = {}
    if token is not None:
        headers['Authorization'] = 'Bearer ' + token
    data = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload).encode()
    remaining = deadline - time.monotonic()
    assert remaining > 0
    req = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        response = opener.open(req, timeout=min(2, remaining))
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read(1024 * 1024 + 1)
        assert len(raw) <= 1024 * 1024
        return response.status, json.loads(raw)
ready = time.monotonic() + 10
while True:
    try:
        code, health = request('/health')
        assert code == 200 and health['ok'] is True and health['build_commit'] == commit
        break
    except (OSError, AssertionError):
        if time.monotonic() >= ready:
            raise RuntimeError('Readiness failed')
        time.sleep(.2)
token = os.environ['CODEX_TOKEN']
for invalid in (None, 'fixture-invalid-only'):
    assert request('/api/hub/v1/overview', invalid)[0] == 401
assert request('/api/hub/v1/auth/login')[0] == 503
if mode == 'seed':
    from engineering_hub.repository import HubRepository
    repo = HubRepository(os.environ['HUB_DB'])
    task = repo.create_task('human:42', 'engineering-hub', 'Disposable durability fixture', 'durability-fixture')
    code, task = request('/api/hub/v1/tasks/' + task['id'] + '/claim', token,
                         {'expected_revision': task['revision']})
    assert code == 200 and task['owner'] == 'codex' and task['status'] == 'claimed'
else:
    assert mode == 'verify'
    task = {'id': sys.argv[3], 'revision': int(sys.argv[4])}
code, overview = request('/api/hub/v1/overview', token)
assert code == 200
matches = [item for item in overview['tasks'] if item['id'] == task['id']]
assert len(matches) == 1
actual = matches[0]
assert actual['revision'] == task['revision'] and actual['owner'] == 'codex' and actual['status'] == 'claimed'
assert overview['capabilities']['host_dispatch'] is False
print(json.dumps({'task_id': actual['id'], 'revision': actual['revision'], 'state': actual['status'],
                  'build_commit': health['build_commit'], 'auth_denials': True}))
'''

BACKUP = r'''
import json, os, stat
from pathlib import Path
from engineering_hub.backup import backup_hub
root = Path('/var/lib/hub')
report = backup_hub(root / 'hub.sqlite3', root / 'backup.sqlite3', timeout_seconds=15)
assert report['quick_check'] == 'ok' and report['permissions'] == '0600'
assert stat.S_IMODE((root / 'backup.sqlite3').stat().st_mode) == 0o600
print(json.dumps(report))
'''

RESTORE = r'''
import hashlib, json, os, shutil, sqlite3, stat
from pathlib import Path
root = Path('/var/lib/hub')
assert os.getuid() == os.getgid() == 10001
assert stat.S_IMODE(root.stat().st_mode) == 0o700
destination = root / 'hub.sqlite3'
fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'wb') as writer, open('/backup-source/backup.sqlite3', 'rb') as reader:
    shutil.copyfileobj(reader, writer, 1024 * 1024)
    writer.flush()
    os.fsync(writer.fileno())
digest = hashlib.sha256()
with destination.open('rb') as reader:
    for chunk in iter(lambda: reader.read(1024 * 1024), b''):
        digest.update(chunk)
with sqlite3.connect((root / 'hub.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
    assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
print(json.dumps({'sha256': digest.hexdigest(), 'size_bytes': destination.stat().st_size,
                  'quick_check': 'ok', 'integrity_check': 'ok'}))
'''


def _docker(args, *, check=True, timeout=30):
    return subprocess.run(['docker', *args], check=check, capture_output=True,
                          text=True, timeout=timeout)


def _volume(name, owner, *, missing_ok=False):
    result = _docker(['volume', 'inspect', '--format',
        '{{.Name}}|{{index .Labels "' + LABEL + '"}}|{{.Driver}}|{{json .Options}}', name], check=False, timeout=15)
    if result.returncode:
        if missing_ok and 'No such volume' in result.stderr:
            return False
        raise RuntimeError('Disposable volume identity unavailable')
    if result.stdout.strip() not in {name + '|' + owner + '|local|null', name + '|' + owner + '|local|{}'}:
        raise RuntimeError('Disposable volume ownership unverified')
    return True


def _container(name, owner, *, missing_ok=False):
    result = _docker(['container', 'inspect', '--format',
        '{{.Id}}|{{index .Config.Labels "' + LABEL + '"}}', name], check=False, timeout=15)
    if result.returncode:
        if missing_ok and ('No such container' in result.stderr or 'No such object' in result.stderr):
            return None
        raise RuntimeError('Disposable container identity unavailable')
    identity, _, actual_owner = result.stdout.strip().partition('|')
    if not re.fullmatch('[a-f0-9]{64}', identity) or actual_owner != owner:
        raise RuntimeError('Disposable container ownership unverified')
    return identity


def _options(name, owner, volume):
    return ['create', '--pull', 'never', '--name', name, '--label', LABEL + '=' + owner,
        '--network', 'none', '--cgroupns', 'private', '--user', '10001:10001', '--read-only', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges', '--pids-limit', '64', '--memory', '512m', '--cpus', '1',
        '--tmpfs', '/tmp:rw,noexec,nosuid,size=32m',
        '--mount', 'type=volume,source=' + volume + ',target=/var/lib/hub',
        '--env', 'HERMES_TOKEN=fixture-only-hermes-not-a-service-token',
        '--env', 'CODEX_TOKEN=fixture-only-codex-not-a-service-token',
        '--env', 'CLAUDE_TOKEN=fixture-only-claude-not-a-service-token',
        '--env', 'HUB_BASE_URL=http://127.0.0.1:8770']


def _profile(identity, expected_volumes):
    # Inspect only public isolation fields, never Config.Env or full metadata.
    template = ('{"user":{{json .Config.User}},"network":{{json .HostConfig.NetworkMode}},'
        '"cgroupns":{{json .HostConfig.CgroupnsMode}},'
        '"readonly":{{json .HostConfig.ReadonlyRootfs}},"privileged":{{json .HostConfig.Privileged}},'
        '"cap_drop":{{json .HostConfig.CapDrop}},"cap_add":{{json .HostConfig.CapAdd}},'
        '"ports":{{json .HostConfig.PortBindings}},"binds":{{json .HostConfig.Binds}},'
        '"devices":{{json .HostConfig.Devices}},"tmpfs":{{json .HostConfig.Tmpfs}},'
        '"memory":{{json .HostConfig.Memory}},"nano_cpus":{{json .HostConfig.NanoCpus}},'
        '"pids_limit":{{json .HostConfig.PidsLimit}},'
        '"security":{{json .HostConfig.SecurityOpt}},"mounts":{{json .Mounts}}}')
    value = json.loads(_docker(['container', 'inspect', '--format', template, identity], timeout=15).stdout)
    if (value.get('user') != '10001:10001' or value.get('network') != 'none'
            or value.get('cgroupns') != 'private'
            or value.get('readonly') is not True or value.get('privileged') is not False
            or value.get('cap_drop') != ['ALL'] or value.get('cap_add') not in (None, [])
            or value.get('ports') not in (None, {}) or value.get('binds') not in (None, [])
            or value.get('devices') not in (None, [])
            or type(value.get('memory')) is not int or value['memory'] != 512 * 1024 ** 2
            or type(value.get('nano_cpus')) is not int or value['nano_cpus'] != 1_000_000_000
            or type(value.get('pids_limit')) is not int or value['pids_limit'] != 64
            or value.get('security') != ['no-new-privileges']):
        raise ValueError('Disposable container isolation unverified')
    tmpfs = value.get('tmpfs')
    if (type(tmpfs) is not dict or set(tmpfs) != {'/tmp'} or type(tmpfs['/tmp']) is not str
            or not {'rw', 'noexec', 'nosuid', 'size=32m'} <= set(tmpfs['/tmp'].split(','))):
        raise ValueError('Disposable temporary storage unverified')
    mounts = value.get('mounts')
    if type(mounts) is not list:
        raise ValueError('Disposable mounts unverified')
    found = {}
    for mount in mounts:
        if type(mount) is not dict:
            raise ValueError('Disposable mounts unverified')
        destination = mount.get('Destination')
        if mount.get('Type') == 'tmpfs' and destination == '/tmp':
            continue  # Some daemon versions also project HostConfig.Tmpfs here.
        if (mount.get('Type') != 'volume' or destination not in expected_volumes
                or destination in found or mount.get('Driver') != 'local'
                or type(mount.get('RW')) is not bool):
            raise ValueError('Disposable mounts unverified')
        found[destination] = (mount.get('Name'), mount['RW'])
    if found != expected_volumes:
        raise ValueError('Disposable mounts unverified')


def _probe(identity, mode, commit, task=None):
    args = ['exec', identity, 'python', '-c', PROBE, mode, commit]
    if task is not None:
        args.extend([task['task_id'], str(task['revision'])])
    result = json.loads(_docker(args).stdout)
    if (type(result) is not dict or set(result) != {'task_id', 'revision', 'state', 'build_commit', 'auth_denials'}
            or type(result['task_id']) is not str or str(uuid.UUID(result['task_id'])) != result['task_id']
            or type(result['revision']) is not int or result['revision'] < 1
            or result['state'] != 'claimed' or result['build_commit'] != commit or result['auth_denials'] is not True):
        raise ValueError('Durability probe result unverified')
    if task is not None and result != task:
        raise ValueError('Durability state changed')
    return result


def _backup_report(value, *, restored=False):
    keys = {'sha256', 'size_bytes', 'quick_check', 'integrity_check' if restored else 'permissions'}
    if (type(value) is not dict or set(value) != keys
            or type(value['sha256']) is not str or re.fullmatch('[0-9a-f]{64}', value['sha256']) is None
            or type(value['size_bytes']) is not int or value['size_bytes'] <= 0
            or value['quick_check'] != 'ok'
            or (value['integrity_check'] != 'ok' if restored else value['permissions'] != '0600')):
        raise ValueError('Backup evidence unverified')
    return value


def run_smoke(image, expected_commit):
    if type(image) is not str or not re.fullmatch('sha256:[a-f0-9]{64}', image):
        raise ValueError('Immutable local image ID required')
    if type(expected_commit) is not str or not re.fullmatch('[a-f0-9]{40}', expected_commit):
        raise ValueError('Exact expected commit required')
    owner = uuid.uuid4().hex
    prefix = 'hub-durability-' + owner
    volumes = [prefix + '-original', prefix + '-restored']
    names = [prefix + '-app', prefix + '-restore', prefix + '-restored-app']
    try:
        for volume in volumes:
            _docker(['volume', 'create', '--driver', 'local', '--label', LABEL + '=' + owner, volume])
            _volume(volume, owner)
        _docker(_options(names[0], owner, volumes[0]) + [image])
        original = _container(names[0], owner)
        _profile(original, {'/var/lib/hub': (volumes[0], True)})
        _docker(['start', original])
        task = _probe(original, 'seed', expected_commit)
        # Restart precisely this same container, not a fresh transient app.
        _docker(['restart', '--time', '10', original])
        _probe(original, 'verify', expected_commit, task)
        backup = _backup_report(json.loads(_docker(['exec', original, 'python', '-c', BACKUP]).stdout))
        _docker(_options(names[1], owner, volumes[1]) + [
            '--mount', 'type=volume,source=' + volumes[0] + ',target=/backup-source,readonly',
            '--entrypoint', 'python', image, '-c', RESTORE])
        helper = _container(names[1], owner)
        _profile(helper, {'/var/lib/hub': (volumes[1], True), '/backup-source': (volumes[0], False)})
        restored = _backup_report(json.loads(_docker(['start', '--attach', helper]).stdout), restored=True)
        if (restored['sha256'] != backup['sha256']
                or restored['size_bytes'] != backup['size_bytes']):
            raise ValueError('Restored database verification failed')
        _docker(_options(names[2], owner, volumes[1]) + [image])
        restored_app = _container(names[2], owner)
        _profile(restored_app, {'/var/lib/hub': (volumes[1], True)})
        _docker(['start', restored_app])
        _probe(restored_app, 'verify', expected_commit, task)
        report = {'durability_smoke': 'passed', 'build_commit': expected_commit,
                  'private_cgroup_namespace': 'effective_configuration_verified',
                  'same_container_restart': 'passed', 'backup_quick_check': 'ok',
                  'restored_integrity_check': 'ok', 'restored_app_state': 'matched',
                  'task_revision': task['revision'], 'auth_denials': 'passed',
                  'human_oauth': 'not_configured', 'host_rollout': 'not_tested',
                  'owner_enrolled_restore': 'not_tested',
                  'truenas_and_machine_reboot': 'not_tested'}
    finally:
        errors = []
        for name in reversed(names):
            try:
                identity = _container(name, owner, missing_ok=True)
                if identity:
                    _docker(['rm', '--force', identity])
            except Exception:
                errors.append('container')
        for volume in reversed(volumes):
            try:
                if _volume(volume, owner, missing_ok=True):
                    _docker(['volume', 'rm', volume])
            except Exception:
                errors.append('volume')
        if errors:
            raise RuntimeError('Disposable cleanup incomplete; unverified resources were not removed') from None
    return report | {'disposable_containers_removed': 3, 'disposable_volumes_removed': 2}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--expected-commit', required=True)
    args = parser.parse_args()
    try:
        result = run_smoke(args.image, args.expected_commit)
    except Exception:
        parser.exit(1, 'Durability smoke failed; inspect disposable resources before retrying.\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
