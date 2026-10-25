"""Bounded runtime observations, never an execution or ownership authority.

Only selected Docker fields and systemd service names are collected. No command,
environment, mounts, ports, logs, filesystem paths or exception text cross this
surface. A root-created optional host snapshot observes the other Docker daemon;
the unprivileged worker never receives that daemon's socket.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time
import uuid

SCHEMA = 'hub-staging-runtime.v1'
MAX_BYTES = 128 * 1024
MAX_ITEMS = 256
MAX_AGE_SECONDS = 90
MAX_FUTURE_SECONDS = 10
DOCKER_FORMAT = ('{"id":{{json .ID}},"name":{{json .Names}},'
                 '"image":{{json .Image}},"state":{{json .State}}}')
DOCKER_ARGS = ('ps', '--no-trunc', '--format', DOCKER_FORMAT)
HOST_DOCKER_COMMAND = ('/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', *DOCKER_ARGS)
HOST_SERVICES_COMMAND = ('/usr/bin/systemctl', 'list-units', '--type=service',
                         '--state=running', '--plain', '--no-legend', '--no-pager', '--output=json')
_ID = re.compile(r'[a-f0-9]{64}\Z')
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_IMAGE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,511}\Z')
_UNIT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@:\\-]{0,247}\.service\Z')
_STATES = frozenset({'running', 'paused', 'restarting'})


def _utc(now=None):
    value = datetime.now(timezone.utc) if now is None else now
    if value.tzinfo is None:
        raise ValueError('timezone_required')
    return value.astimezone(timezone.utc)


def _timestamp(value):
    return _utc(value).isoformat().replace('+00:00', 'Z')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_key')
        result[key] = value
    return result


def _json(raw):
    if type(raw) is not bytes or len(raw) > MAX_BYTES:
        raise ValueError('inventory_size')
    return json.loads(raw, object_pairs_hook=_unique)


def _source(identity, state, observed_at=None):
    return {'id': identity, 'state': state, 'observed_at': observed_at}


def _container(item, scope):
    if (type(item) is not dict or set(item) != {'id', 'name', 'image', 'state'}
            or not all(type(value) is str for value in item.values())
            or not _ID.fullmatch(item['id']) or not _NAME.fullmatch(item['name'])
            or not _IMAGE.fullmatch(item['image']) or item['state'] not in _STATES):
        raise ValueError('invalid_container')
    return {**item, 'scope': scope, 'managed': False}


def parse_containers(raw, scope):
    if type(raw) is not bytes or len(raw) > MAX_BYTES or scope not in {'worker', 'host'}:
        raise ValueError('inventory_size')
    result, identifiers = [], set()
    for line in raw.splitlines():
        if not line.strip():
            continue
        item = _container(_json(line), scope)
        if item['id'] in identifiers or len(result) >= MAX_ITEMS:
            raise ValueError('inventory_records')
        identifiers.add(item['id'])
        result.append(item)
    return sorted(result, key=lambda item: (item['name'], item['id']))


def parse_services(raw):
    rows = _json(raw)
    if type(rows) is not list or len(rows) > MAX_ITEMS:
        raise ValueError('inventory_records')
    result, names = [], set()
    for row in rows:
        # systemctl has additional descriptive fields; none are retained.
        if (type(row) is not dict or type(row.get('unit')) is not str
                or not _UNIT.fullmatch(row['unit']) or row.get('active') != 'active'
                or row.get('sub') != 'running' or row['unit'] in names):
            raise ValueError('invalid_service')
        names.add(row['unit'])
        result.append({'name': row['unit'], 'state': 'running', 'scope': 'host'})
    return sorted(result, key=lambda item: item['name'])


def capture_fixed(command, *, timeout=8, maximum=MAX_BYTES):
    """Capture a fixed trusted child with a bounded reader and monotonic deadline."""
    if tuple(command) not in {HOST_DOCKER_COMMAND, HOST_SERVICES_COMMAND}:
        raise ValueError('fixed_inventory_command_required')
    output, failed, overflow = bytearray(), threading.Event(), threading.Event()
    reader, started, expired = None, False, False
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL,
                               env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent',
                                    'DOCKER_CONFIG': '/nonexistent/eh-observer-no-config',
                                    'SYSTEMD_COLORS': '0', 'SYSTEMD_PAGER': 'cat'},
                               start_new_session=True)
    def drain():
        try:
            while chunk := process.stdout.read(4096):
                available = maximum - len(output)
                output.extend(chunk[:max(0, available)])
                if len(chunk) > available:
                    overflow.set()
        except Exception:
            failed.set()
        finally:
            process.stdout.close()

    try:
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        started = True
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if overflow.is_set() or failed.is_set() or time.monotonic() >= deadline:
                expired = True
                process.kill()  # Only the child created above.
                break
            time.sleep(0.01)
        process.wait(timeout=2)
    except Exception:
        failed.set()
    finally:
        try:
            if process.poll() is None:
                process.kill()
        except Exception:
            failed.set()
        try:
            # Reap even when thread creation/start or the first wait failed.
            process.wait(timeout=2)
        except Exception:
            failed.set()
        finally:
            if started:
                try:
                    reader.join(timeout=2)
                except Exception:
                    failed.set()
            else:
                process.stdout.close()
    if (expired or overflow.is_set() or failed.is_set() or not started
            or reader.is_alive() or process.returncode):
        raise ValueError('inventory_unavailable')
    return bytes(output)


def _empty(now):
    return {'schema': SCHEMA, 'observed_at': _timestamp(now), 'sources': [],
            'containers': [], 'services': []}


def collect_host(*, now=None, capture=capture_fixed):
    now = _utc(now)
    result = _empty(now)
    try:
        containers = parse_containers(capture(HOST_DOCKER_COMMAND), 'host')
        services = parse_services(capture(HOST_SERVICES_COMMAND))
        result.update(containers=containers, services=services,
                      sources=[_source('host', 'observed', _timestamp(now))])
        if len(json.dumps(result, separators=(',', ':')).encode()) > MAX_BYTES:
            raise ValueError('inventory_size')
    except Exception:
        result = _empty(now)
        result['sources'] = [_source('host', 'unavailable')]
    return result


def _secure_directory(path):
    """Return an anchored directory FD, checking every ancestor without symlinks."""
    path = Path(path)
    if os.name != 'posix' or not path.is_absolute() or '..' in path.parts:
        raise ValueError('snapshot_protection')
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        root_info = os.fstat(descriptor)
        if root_info.st_uid != 0 or stat.S_IMODE(root_info.st_mode) & 0o022:
            raise ValueError('snapshot_protection')
        for part in path.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            try:
                info = os.fstat(following)
                if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
                    raise ValueError('snapshot_protection')
            except BaseException:
                os.close(following)
                raise
            os.close(descriptor)
            descriptor = following
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_host(path):
    path = Path(path)
    directory = _secure_directory(path.parent)
    descriptor = None
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o022 or info.st_size > MAX_BYTES):
            raise ValueError('snapshot_protection')
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            raw = stream.read(MAX_BYTES + 1)
        return _json(raw)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def _validate_host(snapshot, now):
    if type(snapshot) is not dict or set(snapshot) != {'schema', 'observed_at', 'sources', 'containers', 'services'}:
        raise ValueError('invalid_snapshot')
    if snapshot['schema'] != SCHEMA or type(snapshot['observed_at']) is not str:
        raise ValueError('invalid_snapshot')
    timestamp = datetime.fromisoformat(snapshot['observed_at'].replace('Z', '+00:00'))
    timestamp = _utc(timestamp)
    age = (now - timestamp).total_seconds()
    if age < -MAX_FUTURE_SECONDS:
        raise ValueError('future_snapshot')
    sources = snapshot['sources']
    if (type(sources) is not list or len(sources) != 1 or type(sources[0]) is not dict
            or set(sources[0]) != {'id', 'state', 'observed_at'} or sources[0]['id'] != 'host'):
        raise ValueError('invalid_snapshot')
    if sources[0]['state'] == 'unavailable' and sources[0]['observed_at'] is None:
        if snapshot['containers'] != [] or snapshot['services'] != []:
            raise ValueError('invalid_snapshot')
        return _source('host', 'unavailable'), [], []
    if sources[0] != _source('host', 'observed', snapshot['observed_at']):
        raise ValueError('invalid_snapshot')
    containers, services = snapshot['containers'], snapshot['services']
    if (type(containers) is not list or type(services) is not list
            or len(containers) > MAX_ITEMS or len(services) > MAX_ITEMS):
        raise ValueError('inventory_records')
    clean_containers = []
    identifiers, names = set(), set()
    for item in containers:
        if type(item) is not dict or set(item) != {'id', 'name', 'image', 'state', 'scope', 'managed'}:
            raise ValueError('invalid_snapshot')
        if item['scope'] != 'host' or item['managed'] is not False:
            raise ValueError('invalid_snapshot')
        row = _container({key: item[key] for key in ('id', 'name', 'image', 'state')}, 'host')
        if row['id'] in identifiers:
            raise ValueError('inventory_records')
        identifiers.add(row['id'])
        clean_containers.append(row)
    for item in services:
        if (type(item) is not dict or set(item) != {'name', 'state', 'scope'} or item['scope'] != 'host'
                or item['state'] != 'running' or type(item['name']) is not str
                or not _UNIT.fullmatch(item['name']) or item['name'] in names):
            raise ValueError('invalid_service')
        names.add(item['name'])
    if age > MAX_AGE_SECONDS:
        return _source('host', 'stale', _timestamp(timestamp)), [], []
    return _source('host', 'observed', _timestamp(timestamp)), clean_containers, services


def collect_runtime(backend, host_snapshot=None, *, now=None):
    now = _utc(now)
    result = _empty(now)
    try:
        result['containers'] = parse_containers(backend._checked_output(DOCKER_ARGS, limit=MAX_BYTES, timeout=8), 'worker')
        result['sources'].append(_source('worker', 'observed', _timestamp(now)))
    except Exception:
        result['sources'].append(_source('worker', 'unavailable'))
    if host_snapshot is None:
        result['sources'].append(_source('host', 'not_configured'))
    else:
        try:
            source, containers, services = _validate_host(_read_host(host_snapshot), now)
            if len(result['containers']) + len(containers) > MAX_ITEMS:
                raise ValueError('inventory_records')
            result['containers'].extend(containers)
            result['services'] = services
            result['sources'].append(source)
        except Exception:
            result['sources'].append(_source('host', 'unavailable'))
    if len(json.dumps(result, separators=(',', ':')).encode()) > MAX_BYTES:
        result = _empty(now)
        result['sources'] = [_source('worker', 'unavailable'), _source('host', 'unavailable')]
    return result


def publish_host(snapshot, path):
    """Atomically replace only a protected observation snapshot, never host state."""
    if getattr(os, 'geteuid', lambda: -1)() != 0:
        raise ValueError('root_collector_required')
    _validate_host(snapshot, _utc())
    data = json.dumps(snapshot, separators=(',', ':')).encode()
    if len(data) > MAX_BYTES:
        raise ValueError('inventory_size')
    path = Path(path)
    directory = _secure_directory(path.parent)
    temporary = '.snapshot-' + str(uuid.uuid4())
    created = False
    try:
        try:
            existing = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (not stat.S_ISREG(existing.st_mode) or existing.st_uid != 0
                or existing.st_nlink != 1 or stat.S_IMODE(existing.st_mode) & 0o022):
            raise ValueError('snapshot_protection')
        if existing is not None:
            # A root-owned regular file is not enough: never overwrite another
            # application's data merely because an operator mistyped --output.
            _validate_host(_read_host(path), _utc())
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory)
        created = True
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if existing is None:
            # Initial publication must not replace a path created concurrently.
            os.link(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory,
                    follow_symlinks=False)
            os.unlink(temporary, dir_fd=directory)
        else:
            current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (existing.st_dev, existing.st_ino):
                raise ValueError('snapshot_changed')
            os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        created = False
        os.fsync(directory)
    finally:
        if created:
            os.unlink(temporary, dir_fd=directory)
        os.close(directory)
