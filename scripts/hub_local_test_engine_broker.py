"""Restricted, local-test-only Docker broker. Never use for staging or production.

The operator and local daemon are trusted. This bounds accidental requests; it
does not establish mount custody against another daemon client. An unfinished
effect permanently disables further effects, including after restart.
"""
from __future__ import annotations

import http.client
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
import uuid
from http.server import BaseHTTPRequestHandler
from urllib.parse import quote

_ID = re.compile(r'[0-9a-f]{64}\Z')
_DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
_NAME = re.compile(r'hub-pilot-[a-f0-9]{24}-[a-f0-9]{8}-[a-f0-9]{3}-(?:candidate|rollback)\Z')
_LABEL = 'engineering-hub.local-test-owner'
_MAX_BODY = 256 * 1024
_MAX_ARCHIVE = 2 * 1024**3
_MAX_STREAM_SECONDS = 120
_REQUEST_TIMEOUT_SECONDS = 10


class Denied(RuntimeError):
    pass


@dataclass(frozen=True)
class ClosedBrokerSnapshot:
    """Local journal observation, not proof of host-wide quiescence."""
    owner: str
    daemon_id: str
    volume_name: str
    volume_mount: str
    enrollment_digest: str
    containers: tuple


@dataclass(frozen=True)
class CleanedBrokerSnapshot(ClosedBrokerSnapshot):
    """Successful broker cleanup observation; volume must still be rechecked."""


def _object(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('duplicate key')
            value[key] = item
        return value
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(value) is not dict:
            raise ValueError()
        return value
    except Exception:
        raise Denied('invalid request') from None


class EffectJournal:
    """Private SQLite journal. No pending effect may ever be replayed."""

    def __init__(self, path):
        path = Path(path)
        if not path.is_absolute() or path.parent.resolve(strict=True) != path.parent:
            raise Denied('invalid journal path')
        parent = os.lstat(path.parent)
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or
                stat.S_IMODE(parent.st_mode) != 0o700):
            raise Denied('invalid journal directory')
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            info = os.lstat(path)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or
                    stat.S_IMODE(info.st_mode) != 0o600):
                raise Denied('invalid journal file') from None
        else:
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5, check_same_thread=False)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS binding (id INTEGER PRIMARY KEY CHECK(id=1), digest TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS control (id INTEGER PRIMARY KEY CHECK(id=1), sealed INTEGER NOT NULL CHECK(sealed IN (0,1)))')
        self.db.execute('INSERT OR IGNORE INTO control VALUES(1,0)')
        self.db.execute('CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS effects (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, effect_key TEXT NOT NULL, state TEXT NOT NULL, UNIQUE(kind,effect_key))')
        self.db.execute('CREATE TABLE IF NOT EXISTS owned (kind TEXT NOT NULL, id TEXT PRIMARY KEY, name TEXT NOT NULL)')

    def bind(self, digest):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT digest FROM binding WHERE id=1').fetchone()
            if row is None:
                self.db.execute('INSERT INTO binding VALUES(1,?)', (digest,))
            elif row != (digest,):
                raise Denied('journal enrollment changed')
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def begin(self, kind, effect_key):
        return self._begin(kind, effect_key, cleanup=False)

    def begin_cleanup(self, kind, effect_key):
        return self._begin(kind, effect_key, cleanup=True)

    def _begin(self, kind, effect_key, *, cleanup):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.db.execute("SELECT 1 FROM effects WHERE state != 'complete' LIMIT 1").fetchone():
                raise Denied('prior effect is held')
            if self.is_sealed() is not cleanup:
                raise Denied('broker admission closed')
            if self.db.execute('SELECT count(*) FROM effects').fetchone()[0] >= 128:
                raise Denied('test effect budget exhausted')
            cursor = self.db.execute("INSERT INTO effects(kind,effect_key,state) VALUES (?,?,'pending')", (kind,effect_key))
            self.db.execute('COMMIT')
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            self.db.execute('ROLLBACK')
            raise Denied('effect was already attempted') from None
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def complete(self, effect, *, kind=None, identity=None, name=None):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if kind is not None:
                self.db.execute('INSERT INTO owned(kind,id,name) VALUES (?,?,?)', (kind, identity, name))
            result = self.db.execute("UPDATE effects SET state='complete' WHERE id=? AND state='pending'", (effect,))
            if result.rowcount != 1:
                raise Denied('effect state changed')
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def owned(self, kind, identity):
        return self.db.execute('SELECT name FROM owned WHERE kind=? AND id=?', (kind, identity)).fetchone()

    def hold(self, effect):
        self.db.execute("UPDATE effects SET state='held' WHERE id=?", (effect,))

    def is_sealed(self):
        row = self.db.execute('SELECT sealed FROM control WHERE id=1').fetchone()
        if row not in ((0,), (1,)):
            raise Denied('broker journal control unavailable')
        return row == (1,)

    def sealed_inventory(self):
        if not self.is_sealed() or self.db.execute("SELECT 1 FROM effects WHERE state != 'complete' LIMIT 1").fetchone():
            raise Denied('broker cleanup is held')
        return tuple(self.db.execute("SELECT id,name FROM owned WHERE kind='container' ORDER BY id").fetchall())

    def transfer_started(self):
        return self.db.execute("SELECT 1 FROM effects WHERE kind='baseline_transfer_out' LIMIT 1").fetchone() is not None

    def seal(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.is_sealed() or self.db.execute("SELECT 1 FROM effects WHERE state != 'complete' LIMIT 1").fetchone():
                raise Denied('broker cannot close with an uncertain effect')
            rows = self.db.execute("SELECT id,name FROM owned WHERE kind='container' ORDER BY id").fetchall()
            if len(rows) > 3 or any(type(identity) is not str or not _ID.fullmatch(identity) or
                                    type(name) is not str or not _NAME.fullmatch(name) for identity, name in rows):
                raise Denied('broker owned inventory unavailable')
            self.db.execute('UPDATE control SET sealed=1 WHERE id=1')
            self.db.execute('COMMIT')
            return tuple(rows)
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def reserve_archive(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute("SELECT value FROM counters WHERE name='archive'").fetchone()
            count = 0 if row is None else row[0]
            if type(count) is not int or not 0 <= count < 12:
                raise Denied('test archive budget exhausted')
            self.db.execute("INSERT INTO counters(name,value) VALUES('archive',1) "
                            "ON CONFLICT(name) DO UPDATE SET value=value+1")
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def close(self):
        self.db.close()


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout, total_timeout):
        super().__init__('docker', timeout=timeout)
        self.path = path
        self.total_timeout = total_timeout
        self._watchdog = None

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        owned_socket = self.sock
        def expire():
            try: owned_socket.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        self._watchdog = threading.Timer(self.total_timeout, expire)
        self._watchdog.daemon = True
        self._watchdog.start()
        self.sock.connect(self.path)

    def close(self):
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog.join()
            self._watchdog = None
        super().close()


class LocalTestBroker:
    """Closed pre-enrollment for the two exact create bodies and compiled probes.

    `volume` must come from the separately verified task-volume helper;
    it is not evidence against concurrent daemon or host writers. Explicit
    post-seal cleanup can remove verified owned containers; it never removes a
    volume or adopts an unknown create result. A created-but-not-started
    container is conservatively retained for inspection.
    """

    def __init__(self, *, socket_path, socket_identity, version, owner,
                 volume, images, creates, exec_bodies, journal):
        from scripts.hub_local_test_volume import OwnedVolume
        if (socket_path != '/var/run/docker.sock' or
                not re.fullmatch(r'1\.[0-9]{2,3}', version) or
                type(owner) is not str or str(uuid.UUID(owner)) != owner or
                type(socket_identity) is not tuple or len(socket_identity) != 2 or
                type(volume) is not OwnedVolume or volume.owner != owner or
                volume.name != 'hub-local-test-' + owner or
                type(volume.mountpoint) is not str or not volume.mountpoint.startswith('/') or
                str(Path(volume.mountpoint)) != volume.mountpoint or '..' in Path(volume.mountpoint).parts or
                not volume.mountpoint.endswith('/volumes/' + volume.name + '/_data') or
                type(images) is not dict or type(creates) is not dict or type(exec_bodies) is not list or
                type(journal) is not EffectJournal):
            raise Denied('invalid test enrollment')
        if len(images) != 2 or len(creates) != 3 or not 1 <= len(exec_bodies) <= 32:
            raise Denied('invalid test enrollment')
        # Each tuple is (approved image config digest, approved platform-manifest digest).
        if any(type(k) is not str or not _DIGEST.fullmatch(k) or type(v) is not tuple or len(v) != 2 or
               any(type(digest) is not str or not _DIGEST.fullmatch(digest) for digest in v)
               for k, v in images.items()):
            raise Denied('invalid test images')
        self.socket_path, self.socket_identity = socket_path, socket_identity
        volume_mount = volume.mountpoint
        self.version, self.owner, self.mount, self.images = version, owner, volume_mount, dict(images)
        self.volume = volume
        self.journal = journal
        self._lock = threading.RLock()
        self._listener = None
        self._listener_closed = False
        self.creates = {}
        for name, body in creates.items():
            if (type(name) is not str or not _NAME.fullmatch(name) or not name.startswith('hub-pilot-' + uuid.UUID(owner).hex[:24] + '-') or
                    type(body) is not dict or set(body) != {'Image', 'User', 'Env', 'HostConfig'} or
                    body.get('Image') not in images or body.get('User') != '10001:10001'):
                raise Denied('invalid create enrollment')
            environment = body['Env']
            allowed_env = {'CODEX_TOKEN', 'HERMES_TOKEN', 'CLAUDE_TOKEN', 'HUB_BASE_URL', 'HUB_DB',
                           'PATH', 'LANG', 'GPG_KEY', 'PYTHON_VERSION', 'PYTHON_SHA256',
                           'PYTHONDONTWRITEBYTECODE', 'PYTHONUNBUFFERED', 'HUB_UI_DIRECTORY',
                           'OPENBLAS_NUM_THREADS', 'DEFAULT_MODE'}
            if (type(environment) is not list or not 5 <= len(environment) <= len(allowed_env) or
                    any(type(item) is not str or len(item) > 4096 or '\x00' in item for item in environment) or
                    len({item.partition('=')[0] for item in environment}) != len(environment) or
                    not {'CODEX_TOKEN', 'HERMES_TOKEN', 'CLAUDE_TOKEN', 'HUB_DB', 'HUB_BASE_URL'} <=
                        {item.partition('=')[0] for item in environment} <= allowed_env or
                    any(not item.partition('=')[1] or not item.partition('=')[2] for item in environment)):
                raise Denied('invalid create environment')
            values = dict(item.split('=', 1) for item in environment)
            if values['HUB_DB'] != '/var/lib/hub/hub.sqlite3' or values['HUB_BASE_URL'] != 'http://127.0.0.1:8770':
                raise Denied('invalid create environment')
            if 'DEFAULT_MODE' in values and values['DEFAULT_MODE'] not in ('fixture-candidate', 'fixture-rollback'):
                raise Denied('invalid test image environment')
            if any(not values[key].startswith('synthetic-') for key in ('CODEX_TOKEN', 'HERMES_TOKEN', 'CLAUDE_TOKEN')):
                raise Denied('only synthetic test credentials are accepted')
            host = body.get('HostConfig')
            fields = {'ReadonlyRootfs', 'CapDrop', 'SecurityOpt', 'Privileged', 'CgroupnsMode', 'NetworkMode',
                      'PortBindings', 'PublishAllPorts', 'Memory', 'MemorySwap', 'NanoCpus', 'PidsLimit',
                      'Tmpfs', 'Mounts', 'RestartPolicy', 'AutoRemove'}
            if (type(host) is not dict or set(host) != fields or host.get('NetworkMode') != 'none' or
                    host.get('Privileged') is not False or host.get('ReadonlyRootfs') is not True or
                    host.get('CapDrop') != ['ALL'] or host.get('SecurityOpt') != ['no-new-privileges:true'] or
                    host.get('CgroupnsMode') != 'private' or host.get('PortBindings') != {} or
                    host.get('PublishAllPorts') is not False or host.get('RestartPolicy') != {'Name': 'no'} or
                    host.get('AutoRemove') is not False or
                    type(host.get('Memory')) is not int or not 1 <= host['Memory'] <= 2 * 1024**3 or
                    host.get('MemorySwap') != host['Memory'] or type(host.get('NanoCpus')) is not int or
                    not 1 <= host['NanoCpus'] <= 1_000_000_000 or type(host.get('PidsLimit')) is not int or
                    not 1 <= host['PidsLimit'] <= 256):
                raise Denied('invalid create enrollment')
            tmpfs = host['Tmpfs']
            if (type(tmpfs) is not dict or set(tmpfs) != {'/tmp'} or type(tmpfs['/tmp']) is not str or
                    not re.fullmatch(r'rw,noexec,nosuid,size=[1-9][0-9]{0,8},mode=1[0-7]{3}', tmpfs['/tmp'])):
                raise Denied('invalid test tmpfs')
            mounts = host.get('Mounts')
            if (type(mounts) is not list or len(mounts) != 1 or type(mounts[0]) is not dict or
                    set(mounts[0]) != {'Type', 'Source', 'Target', 'ReadOnly'} or
                    mounts[0]['Type'] != 'bind' or mounts[0]['Target'] != '/var/lib/hub' or
                    mounts[0]['ReadOnly'] is not False):
                raise Denied('invalid create mount')
            source = mounts[0]['Source']
            if (type(source) is not str or str(Path(source)) != source or '..' in Path(source).parts or
                    str(Path(source).parent) != volume_mount + '/generations' or
                    not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', Path(source).name)):
                raise Denied('invalid create mount')
            self.creates[name] = json.loads(json.dumps(body, allow_nan=False))
        baseline_name = 'hub-pilot-' + uuid.UUID(owner).hex[:24] + '-00000000-000-rollback'
        candidates = [name for name in self.creates if name.endswith('-candidate')]
        rollbacks = [name for name in self.creates if name.endswith('-rollback') and name != baseline_name]
        if baseline_name not in self.creates or len(candidates) != 1 or len(rollbacks) != 1:
            raise Denied('invalid test role enrollment')
        by_role = {'baseline': self.creates[baseline_name], 'candidate': self.creates[candidates[0]],
                   'rollback': self.creates[rollbacks[0]]}
        if (candidates[0].rsplit('-', 1)[0] != rollbacks[0].rsplit('-', 1)[0] or
                by_role['baseline']['Image'] != by_role['rollback']['Image'] or
                by_role['baseline']['Image'] == by_role['candidate']['Image'] or
                len({body['HostConfig']['Mounts'][0]['Source'] for body in by_role.values()}) != 3):
            raise Denied('invalid test role enrollment')
        # All three app containers may coexist. Controller, broker, daemon,
        # image preparation, and storage budgets must be checked separately.
        if (sum(body['HostConfig']['Memory'] for body in by_role.values()) > 3 * 1024**3 or
                sum(body['HostConfig']['NanoCpus'] for body in by_role.values()) > 1_000_000_000 or
                sum(body['HostConfig']['PidsLimit'] for body in by_role.values()) > 256):
            raise Denied('aggregate application budget exceeded')
        self.exec_bodies = [json.loads(json.dumps(body, allow_nan=False)) for body in exec_bodies]
        from hub_container_executor.daemon_backend import _PROBE_SCRIPT
        if any(type(body) is not dict or set(body) != {'AttachStdin', 'AttachStdout', 'AttachStderr', 'Tty', 'User', 'Cmd'}
               or body['AttachStdin'] is not False or body['AttachStdout'] is not True
               or body['AttachStderr'] is not False or body['Tty'] is not False or body['User'] != '10001:10001'
               or type(body['Cmd']) is not list or len(body['Cmd']) != 11
               or body['Cmd'][:4] != ['/usr/local/bin/python3', '-I', '-c', _PROBE_SCRIPT]
               or any(type(arg) is not str or len(arg) > 4096 or '\x00' in arg for arg in body['Cmd'][4:])
               for body in self.exec_bodies):
            raise Denied('invalid probe enrollment')
        self._binding = self._current_binding()
        journal.bind(self._binding)

    def _current_binding(self):
        enrollment = {'owner': self.owner, 'daemon_id': self.volume.daemon_id, 'volume': self.volume.name,
                      'mountpoint': self.volume.mountpoint, 'version': self.version,
                      'socket_path': self.socket_path, 'socket_identity': self.socket_identity, 'images': self.images,
                      'creates': self.creates, 'exec_bodies': self.exec_bodies}
        return hashlib.sha256(json.dumps(enrollment, sort_keys=True, separators=(',', ':'),
                                          ensure_ascii=True, allow_nan=False).encode()).hexdigest()

    def _connection(self, timeout, total_timeout):
        st = os.lstat(self.socket_path)
        if not stat.S_ISSOCK(st.st_mode) or (st.st_dev, st.st_ino) != self.socket_identity:
            raise Denied('daemon socket changed')
        return UnixHTTPConnection(self.socket_path, timeout, total_timeout)

    def _request(self, method, target, body=None, *, timeout=15, total_timeout=15):
        connection = self._connection(timeout, total_timeout)
        try:
            data = None if body is None else json.dumps(body, separators=(',', ':'), allow_nan=False).encode()
            connection.request(method, target, body=data, headers={'Content-Type': 'application/json', 'Accept-Encoding': 'identity'})
            return connection, connection.getresponse()
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _read_bounded(response, maximum):
        """Bound total response time even when a daemon drips bytes forever."""
        sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
        if sock is None:
            raise Denied('response deadline unavailable')
        started = time.monotonic()
        def expire():
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        watchdog = threading.Timer(15, expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            parts, total = [], 0
            while True:
                remaining = 15 - (time.monotonic() - started)
                if remaining <= 0:
                    raise Denied('response deadline')
                sock.settimeout(min(10, remaining))
                chunk = response.read1(min(65536, maximum - total + 1))
                if time.monotonic() - started >= 15:
                    raise Denied('response deadline')
                if not chunk:
                    return b''.join(parts)
                total += len(chunk)
                if total > maximum:
                    raise Denied('response too large')
                parts.append(chunk)
                if response.isclosed():
                    return b''.join(parts)
        finally:
            watchdog.cancel()
            watchdog.join()

    def _inspect_owned(self, identity, name, expected_status):
        connection, response = self._request('GET', f'/v{self.version}/containers/{identity}/json')
        try:
            if response.status != 200:
                raise Denied('owned container unavailable')
            raw = self._read_bounded(response, _MAX_BODY)
            actual = _object(raw)
            expected = self.creates[name]
            config, host = actual.get('Config'), actual.get('HostConfig')
            mounts = actual.get('Mounts')
            state = actual.get('State')
            want_mount = expected['HostConfig']['Mounts'][0]
            allowed_status = (expected_status,) if type(expected_status) is str else expected_status
            if (actual.get('Id') != identity or actual.get('Image') not in self.images[expected['Image']] or
                    type(config) is not dict or config.get('User') != expected['User'] or config.get('Image') != expected['Image'] or
                    type(config.get('Env')) is not list or sorted(config['Env']) != sorted(expected['Env']) or
                    type(config.get('Labels')) is not dict or config['Labels'].get(_LABEL) != self.owner or
                    type(host) is not dict or type(state) is not dict or state.get('Status') not in allowed_status or
                    type(mounts) is not list or len(mounts) not in (1, 2) or type(mounts[0]) is not dict or
                    mounts[0].get('Type') != 'bind' or
                    mounts[0].get('Source') != want_mount['Source'] or
                    mounts[0].get('Destination') != want_mount['Target'] or mounts[0].get('RW') is not True or
                    mounts[0].get('Propagation') not in ('', 'rprivate') or mounts[0].get('Mode') not in ('', 'rw')):
                raise Denied('owned container identity mismatch')
            if state.get('Status') == 'exited' and (
                    state.get('Running') is not False or type(state.get('Pid')) is not int or state['Pid'] != 0 or
                    state.get('Paused') is not False or state.get('Restarting') is not False or state.get('Dead') is not False):
                raise Denied('exited container process state unavailable')
            if (len(mounts) == 2 and (type(mounts[1]) is not dict or mounts[1].get('Type') != 'tmpfs' or
                    mounts[1].get('Destination') != '/tmp' or mounts[1].get('Source') not in (None, '') or
                    mounts[1].get('RW') is not True or mounts[1].get('Propagation') not in (None, ''))):
                raise Denied('unexpected owned container mount')
            for key in ('ReadonlyRootfs', 'CapDrop', 'SecurityOpt', 'Privileged', 'CgroupnsMode',
                        'NetworkMode', 'Memory', 'MemorySwap', 'NanoCpus', 'PidsLimit', 'AutoRemove'):
                if host.get(key) != expected['HostConfig'].get(key):
                    raise Denied('owned container profile mismatch')
            for key in ('CapAdd', 'GroupAdd', 'Binds', 'Devices', 'DeviceRequests', 'DeviceCgroupRules',
                        'ExtraHosts', 'Links', 'VolumesFrom'):
                if host.get(key) not in (None, []):
                    raise Denied('owned container profile mismatch')
            if host.get('PidMode') not in (None, '', 'private') or host.get('IpcMode') not in (None, '', 'private'):
                raise Denied('owned container profile mismatch')
            if (host.get('Tmpfs') != expected['HostConfig']['Tmpfs'] or
                    type(host.get('RestartPolicy')) is not dict or host['RestartPolicy'].get('Name') != 'no' or
                    host.get('PortBindings') not in (None, {}) or host.get('PublishAllPorts') is not False):
                raise Denied('owned container profile mismatch')
            configured = host.get('Mounts')
            if (type(configured) is not list or len(configured) != 1 or type(configured[0]) is not dict or
                    set(configured[0]) - {'Type', 'Source', 'Target', 'ReadOnly'} or
                    configured[0].get('Type') != 'bind' or configured[0].get('Source') != want_mount['Source'] or
                    configured[0].get('Target') != want_mount['Target'] or configured[0].get('ReadOnly', False) is not False):
                raise Denied('owned container configured mount mismatch')
            return state['Status']
        finally:
            connection.close()

    def _attest_volume(self):
        """Recheck exact daemon and task volume; no host-custody claim."""
        for target, kind in ((f'/v{self.version}/info', 'info'),
                             (f'/v{self.version}/volumes/{self.volume.name}', 'volume')):
            connection, response = self._request('GET', target)
            try:
                raw = self._read_bounded(response, _MAX_BODY)
                if response.status != 200:
                    raise Denied('test volume unavailable')
                value = _object(raw)
                if kind == 'info':
                    root = value.get('DockerRootDir')
                    if (value.get('ID') != self.volume.daemon_id or type(root) is not str or not root.startswith('/') or
                            root.startswith('//') or str(Path(root)) != root or '..' in Path(root).parts or
                            self.volume.mountpoint != root + '/volumes/' + self.volume.name + '/_data'):
                        raise Denied('daemon changed')
                elif (value.get('Name') != self.volume.name or value.get('Driver') != 'local' or
                      value.get('Scope') != 'local' or value.get('Mountpoint') != self.volume.mountpoint or
                      value.get('Labels') != {_LABEL: self.owner} or value.get('Options') not in (None, {})):
                    raise Denied('test volume changed')
            finally:
                connection.close()

    def dispatch(self, method, target, raw, emit):
        """Forward one closed request; emit(status, headers, chunks) streams GETs."""
        with self._lock:
            if self.journal.is_sealed():
                raise Denied('broker admission closed')
            return self._dispatch(method, target, raw, emit)

    def seal(self):
        """Drain the serial request and permanently close broker admission.

        The caller must also stop/join the UDS server, confirm every recorded
        container is absent on the pinned daemon, and separately reserve any
        volume deletion. Never infer host-wide quiescence from this snapshot.
        """
        with self._lock:
            if not hmac.compare_digest(self._binding, self._current_binding()):
                raise Denied('test enrollment changed')
            rows = self.journal.seal()
            if any(name not in self.creates for _, name in rows):
                raise Denied('broker inventory changed; preserve resources')
            return ClosedBrokerSnapshot(self.owner, self.volume.daemon_id, self.volume.name,
                                        self.volume.mountpoint, self._binding, rows)

    def cleanup_owned(self, snapshot):
        """Gracefully stop and remove only sealed, exactly reverified IDs.

        A lost response, changed resource, or unjoined server retains all
        remaining resources. This does not remove the task volume or assert
        host-wide quiescence. No force or automatic retry is used.
        """
        with self._lock:
            if (type(snapshot) is not ClosedBrokerSnapshot or self._listener is None or not self._listener_closed or
                    snapshot.owner != self.owner or snapshot.daemon_id != self.volume.daemon_id or
                    snapshot.volume_name != self.volume.name or snapshot.volume_mount != self.volume.mountpoint or
                    snapshot.enrollment_digest != self._binding or
                    self.journal.transfer_started() or
                    not hmac.compare_digest(self._binding, self._current_binding()) or
                    snapshot.containers != self.journal.sealed_inventory()):
                raise Denied('broker cleanup boundary unavailable')
            for identity, name in snapshot.containers:
                self._attest_volume()
                connection, response = self._request('GET', f'/v{self.version}/containers/{identity}/json')
                try:
                    status = response.status
                    self._read_bounded(response, _MAX_BODY)
                finally:
                    connection.close()
                if status == 404:
                    continue
                if status != 200:
                    raise Denied('owned container status unavailable')
                state = self._inspect_owned(identity, name, ('running', 'exited'))
                if state == 'running':
                    self._cleanup_effect('cleanup_stop', identity, name)
                self._inspect_owned(identity, name, 'exited')
                self._cleanup_effect('cleanup_delete', identity, name)
            # Independent exact-ID absence is rechecked by the volume helper.
            if self.journal.sealed_inventory() != snapshot.containers:
                raise Denied('cleanup journal changed; preserve resources')
            return CleanedBrokerSnapshot(snapshot.owner, snapshot.daemon_id, snapshot.volume_name,
                                         snapshot.volume_mount, snapshot.enrollment_digest, snapshot.containers)

    def transfer_baseline_from(self, *, source, snapshot, manifest, rollback_image, provider):
        """Transfer one exact baseline between sealed/bootstrap and run brokers.

        Only same-process trusted fixture composition is supported. A protected
        receipt remains a point-in-time observation, not host custody. A crash
        or uncertainty in either journal holds both and is never auto-replayed.
        """
        from engineering_hub.container_pilot_manifest import PilotManifest
        from hub_container_executor.adapter import HostAdapter
        from hub_container_executor.backend import BaselineIdentity, ContainerPlan
        from hub_container_executor.baseline_receipt import ProtectedBaselineProvider
        from hub_container_executor.build_record import VerifiedImage
        from hub_container_executor.daemon_backend import DockerEngineBackend
        from hub_container_executor.runtime_profile import derive_runtime_profile, runtime_profile_digest
        if (type(source) is not LocalTestBroker or source is self or type(snapshot) is not ClosedBrokerSnapshot or
                type(manifest) is not PilotManifest or type(rollback_image) is not VerifiedImage or
                rollback_image.role != 'rollback' or type(provider) is not ProtectedBaselineProvider or
                source.journal is self.journal):
            raise Denied('baseline transfer unavailable')
        # The fixture owns both brokers and must call in this one fixed order.
        with source._lock, self._lock:
            if (not source._listener_closed or source._listener is None or source.journal.transfer_started() or
                    source.owner != self.owner or source.volume != self.volume or source.version != self.version or
                    source.socket_path != self.socket_path or source.socket_identity != self.socket_identity or
                    source.images != self.images or source.exec_bodies != self.exec_bodies or
                    not hmac.compare_digest(source._binding, source._current_binding()) or
                    not hmac.compare_digest(self._binding, self._current_binding()) or
                    snapshot.owner != source.owner or snapshot.daemon_id != source.volume.daemon_id or
                    snapshot.volume_name != source.volume.name or snapshot.volume_mount != source.volume.mountpoint or
                    snapshot.enrollment_digest != source._binding or
                    snapshot.containers != source.journal.sealed_inventory() or len(snapshot.containers) != 1 or
                    self.journal.is_sealed() or self.journal.db.execute('SELECT 1 FROM effects LIMIT 1').fetchone() or
                    self.journal.db.execute('SELECT 1 FROM owned LIMIT 1').fetchone()):
                raise Denied('baseline transfer boundary unavailable')
            identity, name = snapshot.containers[0]
            reserved = 'hub-pilot-' + uuid.UUID(self.owner).hex[:24] + '-00000000-000-rollback'
            if (name != reserved or manifest.runtime.resource_name_prefix != 'hub-pilot-' + uuid.UUID(self.owner).hex[:24] or
                    name not in source.creates or name not in self.creates or
                    source.creates[name] != self.creates[name] or
                    self.creates[name]['Image'] != rollback_image.runnable_reference or
                    self.images.get(rollback_image.runnable_reference) !=
                        (rollback_image.image_config_id, rollback_image.platform_manifest_digest) or
                    manifest.rollback.image_config_id != rollback_image.image_config_id or
                    manifest.rollback.source_commit != rollback_image.source_commit):
                raise Denied('baseline transfer image mismatch')
            baseline = provider.enrolled_baseline(manifest)
            if (type(baseline) is not BaselineIdentity or provider.verify_baseline(baseline, manifest) is not True or
                    baseline.container_id != identity or baseline.image_config_id != rollback_image.image_config_id or
                    baseline.state_generation_id != Path(self.creates[name]['HostConfig']['Mounts'][0]['Source']).name):
                raise Denied('baseline transfer receipt mismatch')
            values = dict(item.split('=', 1) for item in self.creates[name]['Env'])
            tokens = {key: values[key] for key in ('CODEX_TOKEN', 'HERMES_TOKEN', 'CLAUDE_TOKEN')}
            profile = derive_runtime_profile(manifest, HostAdapter._defaults_from_record(rollback_image),
                                             baseline.state_generation_id, tokens)
            profile_digest = runtime_profile_digest(profile)
            backend = object.__new__(DockerEngineBackend)
            backend._gate_mount_verifier = None
            plan = ContainerPlan(name, rollback_image, rollback_image.runnable_reference, False, profile)
            source_path = Path(self.creates[name]['HostConfig']['Mounts'][0]['Source'])
            if (provider._profile != profile_digest or
                    backend._create_body(plan, source_path, profile.environment) != self.creates[name]):
                raise Denied('baseline transfer profile mismatch')
            source_ticket = source.journal.begin_cleanup('baseline_transfer_out', identity)
            target_ticket = self.journal.begin('baseline_transfer_in', identity)
            source._attest_volume(); self._attest_volume()
            source._inspect_owned(identity, name, 'running')
            self._inspect_owned(identity, name, 'running')
            if (provider.verify_baseline(baseline, manifest) is not True or provider._profile != profile_digest or
                    not hmac.compare_digest(source._binding, source._current_binding()) or
                    not hmac.compare_digest(self._binding, self._current_binding())):
                raise Denied('baseline transfer inputs changed')
            source.journal.complete(source_ticket)
            self.journal.complete(target_ticket, kind='container', identity=identity, name=name)
            return baseline

    def _cleanup_effect(self, kind, identity, name):
        self._attest_volume()
        ticket = self.journal.begin_cleanup(kind, identity)
        connection = None
        try:
            target = f'/v{self.version}/containers/{identity}'
            method = 'POST' if kind == 'cleanup_stop' else 'DELETE'
            target += '/stop?t=5' if kind == 'cleanup_stop' else '?force=0&v=0'
            connection, response = self._request(method, target)
            self._read_bounded(response, _MAX_BODY)
            if response.status != 204:
                raise Denied('cleanup outcome uncertain')
            connection.close(); connection = None
            if kind == 'cleanup_stop':
                self._inspect_owned(identity, name, 'exited')
            else:
                connection, response = self._request('GET', f'/v{self.version}/containers/{identity}/json')
                self._read_bounded(response, _MAX_BODY)
                if response.status != 404:
                    raise Denied('container removal unverified')
            self.journal.complete(ticket)
        finally:
            if connection is not None:
                connection.close()

    def _dispatch(self, method, target, raw, emit):
        try:
            if not hmac.compare_digest(self._binding, self._current_binding()):
                raise Denied('test enrollment changed')
        except (TypeError, ValueError, AttributeError):
            raise Denied('test enrollment changed') from None
        if type(raw) is not bytes or len(raw) > _MAX_BODY or type(target) is not str or '#' in target:
            raise Denied('invalid request')
        prefix = f'/v{self.version}'
        image_paths = {prefix + '/images/' + quote(digest, safe='') + '/' + suffix
                       for digest in self.images for suffix in ('json', 'get')}
        if method == 'GET' and (target == prefix + '/info' or target in image_paths):
            if raw:
                raise Denied('GET body rejected')
            if target.endswith('/get'):
                self.journal.reserve_archive()
            return self._stream(method, target, emit, _MAX_ARCHIVE if target.endswith('/get') else _MAX_BODY)
        if method == 'GET' and target in {prefix + '/containers/' + quote(n, safe='') + '/json' for n in self.creates}:
            if raw:
                raise Denied('GET body rejected')
            return self._stream(method, target, emit, _MAX_BODY)
        match = re.fullmatch(re.escape(prefix) + r'/containers/([a-f0-9]{64})/(json|start|stop|exec)(?:\?t=5)?', target)
        if match:
            identity, action = match.groups()
            owned = self.journal.owned('container', identity)
            if owned is None:
                raise Denied('container is not owned')
            name = owned[0]
            if method == 'GET' and action == 'json' and target.endswith('/json') and not raw:
                return self._stream(method, target, emit, _MAX_BODY)
            if method != 'POST' or action not in ('start', 'stop', 'exec'):
                raise Denied('operation denied')
            if (action == 'stop') != target.endswith('?t=5'):
                raise Denied('operation denied')
            body = _object(raw) if raw else None
            if action == 'exec':
                if body not in self.exec_bodies:
                    raise Denied('probe denied')
            elif body is not None:
                raise Denied('operation body denied')
            effect_kind = action
            if action == 'start':
                baseline = 'hub-pilot-' + uuid.UUID(self.owner).hex[:24] + '-00000000-000-rollback'
                started = self.journal.db.execute("SELECT state FROM effects WHERE kind='start' AND effect_key=?", (identity,)).fetchone()
                if name == baseline and started == ('complete',):
                    created = self.journal.db.execute("SELECT state FROM effects WHERE kind='create' AND effect_key=?", (name,)).fetchone()
                    stopped = self.journal.db.execute("SELECT state FROM effects WHERE kind='stop' AND effect_key=?", (identity,)).fetchone()
                    if created != ('complete',) or stopped != ('complete',):
                        raise Denied('baseline create or stop is not complete')
                    unstarted = self.journal.db.execute(
                        "SELECT 1 FROM owned o WHERE o.kind='exec' AND o.name=? AND NOT EXISTS "
                        "(SELECT 1 FROM effects e WHERE e.kind='exec_start' AND e.effect_key=o.id AND e.state='complete') LIMIT 1",
                        (identity,)).fetchone()
                    if unstarted is not None:
                        raise Denied('baseline probe execution is incomplete')
                    self._inspect_owned(identity, name, 'exited')
                    effect_kind = 'baseline_restart'
                else:
                    self._inspect_owned(identity, name, 'created')
            else:
                self._inspect_owned(identity, name, 'running')
            probe_key = (identity + ':' + hashlib.sha256(json.dumps(body, sort_keys=True,
                separators=(',', ':'), allow_nan=False).encode()).hexdigest()) if action == 'exec' else identity
            if action == 'exec':
                baseline = 'hub-pilot-' + uuid.UUID(self.owner).hex[:24] + '-00000000-000-rollback'
                first = self.journal.db.execute("SELECT state FROM effects WHERE kind='exec' AND effect_key=?", (probe_key,)).fetchone()
                restarted = self.journal.db.execute("SELECT state FROM effects WHERE kind='baseline_restart' AND effect_key=?", (identity,)).fetchone()
                if name == baseline and first == ('complete',) and restarted == ('complete',):
                    probe_key += ':post-restart'
            return self._effect(effect_kind, method, target, body, emit, name=identity if action == 'exec' else name,
                                effect_key=probe_key)
        match = re.fullmatch(re.escape(prefix) + r'/exec/([a-f0-9]{64})/(start|json)', target)
        if match:
            identity, action = match.groups()
            owned_exec = self.journal.owned('exec', identity)
            if owned_exec is None:
                raise Denied('exec is not owned')
            if method == 'GET' and action == 'json' and not raw:
                return self._stream(method, target, emit, _MAX_BODY)
            if method == 'POST' and action == 'start' and _object(raw) == {'Detach': False, 'Tty': False}:
                container_id = owned_exec[0]
                container = self.journal.owned('container', container_id)
                if container is None:
                    raise Denied('exec container is unavailable')
                self._inspect_owned(container_id, container[0], 'running')
                return self._effect('exec_start', method, target, _object(raw), emit, effect_key=identity)
            raise Denied('exec operation denied')
        prefix_create = prefix + '/containers/create?name='
        if method == 'POST' and target.startswith(prefix_create):
            name = target[len(prefix_create):]
            if name not in self.creates or _object(raw) != self.creates[name]:
                raise Denied('create denied')
            body = dict(self.creates[name]); body['Labels'] = {_LABEL: self.owner}
            return self._effect('create', method, target, body, emit, name=name, effect_key=name)
        raise Denied('route denied')

    def _effect(self, kind, method, target, body, emit, *, name=None, effect_key=None):
        # Mutations, including identical probe creates, are one-shot. This may
        # conservatively hold a readiness retry; it never authorizes a replay.
        if effect_key is None:
            effect_key = os.urandom(16).hex()
        self._attest_volume()
        ticket = self.journal.begin(kind, effect_key)
        connection = None
        try:
            connection, response = self._request(method, target, body)
            expected = 201 if kind in ('create', 'exec') else 200 if kind == 'exec_start' else 204
            limit = _MAX_BODY if kind != 'exec_start' else 1024 * 1024
            raw = self._read_bounded(response, limit)
            if response.status != expected:
                raise Denied('effect outcome uncertain')
            record_kind = identity = None
            if kind in ('create', 'exec'):
                identity = _object(raw).get('Id')
                if type(identity) is not str or not _ID.fullmatch(identity):
                    raise Denied('effect outcome uncertain')
                record_kind = 'container' if kind == 'create' else 'exec'
                if kind == 'create':
                    self._inspect_owned(identity, name, 'created')
            self.journal.complete(ticket, kind=record_kind, identity=identity, name=name)
            try:
                emit(response.status, {'Content-Type': response.getheader('Content-Type', 'application/json')}, iter((raw,)))
            except BaseException:
                self.journal.hold(ticket)
                raise
        finally:
            if connection is not None:
                connection.close()

    def _stream(self, method, target, emit, maximum):
        connection, response = self._request(method, target, timeout=10, total_timeout=_MAX_STREAM_SECONDS)
        started = time.monotonic()
        length = response.getheader('Content-Length')
        if (response.status not in (200, 404) or response.getheader('Content-Encoding', 'identity').lower() not in ('', 'identity') or
                length is not None and (not length.isdecimal() or int(length) > maximum)):
            connection.close(); raise Denied('response too large')
        stream_socket = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
        if stream_socket is None:
            connection.close(); raise Denied('stream deadline unavailable')
        def expire():
            try:
                stream_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        watchdog = threading.Timer(_MAX_STREAM_SECONDS, expire)
        watchdog.daemon = True
        watchdog.start()
        def chunks():
            total = 0
            try:
                while True:
                    remaining = _MAX_STREAM_SECONDS - (time.monotonic() - started)
                    if remaining <= 0:
                        raise Denied('response deadline')
                    stream_socket.settimeout(min(10, remaining))
                    chunk = response.read1(min(65536, maximum - total + 1))
                    if time.monotonic() - started >= _MAX_STREAM_SECONDS:
                        raise Denied('response deadline')
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > maximum:
                        raise Denied('response too large')
                    yield chunk
                    if response.isclosed():
                        break
            finally:
                connection.close()
        try:
            emit(response.status, {'Content-Type': response.getheader('Content-Type', 'application/octet-stream')}, chunks())
        finally:
            watchdog.cancel()
            watchdog.join()
            connection.close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self._header_timer = self._timer()

    def _timer(self, seconds=None):
        def expire():
            try: self.connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        timer = threading.Timer(_REQUEST_TIMEOUT_SECONDS if seconds is None else seconds, expire)
        timer.daemon = True
        timer.start()
        return timer

    def parse_request(self):
        try:
            return super().parse_request()
        finally:
            self._header_timer.cancel()
            self._header_timer.join()

    def finish(self):
        self._header_timer.cancel()
        self._header_timer.join()
        super().finish()

    def log_message(self, *_):
        pass  # Request bodies and daemon responses can contain test credentials.

    def do_GET(self):
        self._serve()

    def do_POST(self):
        self._serve()

    def _serve(self):
        self.close_connection = True
        sent = False
        overall_timer = None
        try:
            names = [key.lower() for key, _ in self.headers.raw_items()]
            if (len(names) != len(set(names)) or 'transfer-encoding' in names or
                    'upgrade' in names or len(self.path) > 1024 or
                    any(ord(char) < 32 or ord(char) == 127 for char in self.path)):
                raise Denied('invalid request framing')
            length = self.headers.get('Content-Length', '0')
            if not length.isdecimal() or int(length) > _MAX_BODY:
                raise Denied('invalid request framing')
            self.connection.settimeout(_REQUEST_TIMEOUT_SECONDS)
            body_timer = self._timer()
            try:
                body = self.rfile.read(int(length))
            finally:
                body_timer.cancel()
                body_timer.join()
            if len(body) != int(length):
                raise Denied('truncated request')
            overall_timer = self._timer(120)
            def emit(status, headers, chunks):
                nonlocal sent
                self.send_response(status)
                self.send_header('Connection', 'close')
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                sent = True
                for chunk in chunks:
                    self.wfile.write(chunk)
            self.server.broker.dispatch(self.command, self.path, body, emit)
        except Exception:
            if not sent:
                try:
                    self.send_response(403)
                    self.send_header('Connection', 'close')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                except OSError:
                    pass
        finally:
            if overall_timer is not None:
                overall_timer.cancel()
                overall_timer.join()


class LocalTestSocketServer(socketserver.UnixStreamServer):
    """Synchronous UDS server; caller owns the thread, journal and teardown."""

    def __init__(self, path, broker):
        if type(broker) is not LocalTestBroker or type(path) is not str or not os.path.isabs(path):
            raise Denied('invalid broker socket')
        if broker._listener is not None or broker._listener_closed:
            raise Denied('broker listener already used')
        parent = Path(path).parent
        info = os.lstat(parent)
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or
                info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700 or
                parent.resolve(strict=True) != parent or os.path.lexists(path)):
            raise Denied('invalid broker socket directory')
        self.broker = broker
        self._owned_path = path
        self._serve_exited = threading.Event()
        self._serve_exited.set()
        super().__init__(path, _Handler)
        os.chmod(path, 0o600)
        current = os.lstat(path)
        self._owned_identity = current.st_dev, current.st_ino
        broker._listener = self

    def serve_forever(self, poll_interval=.5):
        if self.broker._listener_closed:
            raise Denied('broker listener is closed')
        self._serve_exited.clear()
        try:
            return super().serve_forever(poll_interval)
        finally:
            self._serve_exited.set()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(_REQUEST_TIMEOUT_SECONDS)  # Includes request-line and header parsing.
        return connection, address

    def server_close(self):
        if not self._serve_exited.is_set():
            raise Denied('broker listener still serving')
        super().server_close()
        try:
            current = os.lstat(self._owned_path)
        except FileNotFoundError:
            raise Denied('broker socket missing; preserve resources') from None
        if not stat.S_ISSOCK(current.st_mode) or (current.st_dev, current.st_ino) != self._owned_identity:
            raise Denied('broker socket changed; preserve path')
        os.unlink(self._owned_path)
        self.broker._listener_closed = True
