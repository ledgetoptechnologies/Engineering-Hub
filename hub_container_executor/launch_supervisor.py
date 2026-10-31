"""Explicit root-owned, single-launch Docker executor/receipt supervisor.

Nothing runs at import. Independent ROOT-PROTECTED enrollment and an existing
private signing key are mandatory; no key/enrollment is generated here. This is
not a service installer, issuer discovery, retry/recovery worker or permission
provisioner. An actual independently packaged direct-init runner is still needed:
the ordinary Hub image does not contain that runner and is not accepted merely
because its health endpoint works. Same-UID/root administrators remain trusted.
"""
from __future__ import annotations

import base64
import array
from contextlib import contextmanager
from dataclasses import dataclass, InitVar
import fcntl
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import select
import socket
import sqlite3
import stat
import struct
import sys
import threading
import time
from uuid import UUID, uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from engineering_hub.container_pilot_manifest import Platform
from engineering_hub.executor_launch_profile import LAUNCH_TARGETS
from engineering_hub import current_executor_observation as current_observation
from .build_record import VerifiedImage
from .daemon_backend import _archive_metadata, _archive_image
from .runtime_identity import (ExecutorReleaseEnrollment, PROTOCOL, AUDIENCE,
                               SIGNATURE_DOMAIN, MAX_RECEIPT_BYTES)
from .launch_transport import REQUEST_PROTOCOL, MAX_REQUEST_BYTES
from .configuration_commitment import configuration_commitment, coordinator_configuration_commitment


_HEX = re.compile(r'[0-9a-f]{64}\Z')
_SHA = re.compile(r'sha256:[0-9a-f]{64}\Z')
CURRENT_REQUEST_PROTOCOL = 'engineering-hub.executor-current-observation-request.v1'
_TARGETS = LAUNCH_TARGETS


class _ObservationWaitExpired(Exception):
    """Internal control flow: the polling window, not launch authority, ended."""


class LaunchSupervisorError(RuntimeError):
    """Sanitized denial/uncertainty; never reflects key, path, nonce or Engine text."""

    def __init__(self):
        super().__init__('executor launch/issuance unavailable; preserve original evidence')


def _require(value):
    if not value:
        raise LaunchSupervisorError()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def _strict(raw, maximum):
    _require(type(raw) is bytes and 0 < len(raw) <= maximum)
    def pairs(values):
        result = {}
        for key, value in values:
            _require(key not in result)
            result[key] = value
        return result
    def reject(_):
        raise LaunchSupervisorError()
    result = json.loads(raw, object_pairs_hook=pairs, parse_float=reject, parse_constant=reject)
    _require(_canonical(result) == raw)
    return result


def _path(value):
    _require(type(value) is str and value.startswith('/') and value != '/'
             and os.path.normpath(value) == value and not value.startswith('//')
             and not any(ord(char) < 32 or ord(char) == 127 for char in value))
    return Path(value)


def _identity(info):
    return info.st_dev, info.st_ino


def _root_parent(path, *, private):
    """No-follow root-owned ancestor walk; private leaves must be mode0700."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    try:
        for name in path.parent.parts[1:]:
            current = os.fstat(fd)
            _require(current.st_uid == 0 and not current.st_mode & 0o022
                     or current.st_uid == 0 and current.st_mode & stat.S_ISVTX)
            next_fd = os.open(name, flags, dir_fd=fd)
            try:
                named = os.stat(name, dir_fd=fd, follow_symlinks=False)
                _require(_identity(named) == _identity(os.fstat(next_fd)))
            except BaseException:
                os.close(next_fd)
                raise
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        _require(info.st_uid == 0 and stat.S_ISDIR(info.st_mode) and not info.st_mode & 0o022)
        if private:
            _require(stat.S_IMODE(info.st_mode) == 0o700)
        _require(path.parent.resolve(strict=True) == path.parent)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _root_file(path, maximum):
    parent = _root_parent(path, private=True)
    fd = None
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                     dir_fd=parent)
        before = os.fstat(fd)
        _require(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
                 and stat.S_IMODE(before.st_mode) == 0o600 and 0 < before.st_size <= maximum)
        raw = os.read(fd, maximum + 1)
        after = os.fstat(fd)
        stable = lambda value: (_identity(value), value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        _require(stable(before) == stable(after) and _identity(before) == _identity(os.stat(
            path.name, dir_fd=parent, follow_symlinks=False)) and len(raw) == before.st_size)
        return raw, (_identity(before), hashlib.sha256(raw).hexdigest())
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _remaining(deadline):
    value = deadline - time.monotonic()
    _require(math.isfinite(value) and value > 0)
    return value


def _boot_id():
    value = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    _require(str(UUID(value)) == value)
    return value


def _proc_start(pid):
    raw = Path(f'/proc/{pid}/stat').read_text()
    # comm may contain spaces and ')'; fields after its final ')' start at #3.
    suffix = raw[raw.rfind(')') + 2:].split()
    _require(len(suffix) >= 20 and suffix[0] not in {'Z', 'X'})
    ticks = int(suffix[19])
    _require(ticks > 0)
    return ticks


class _Process:
    def __init__(self, pid, *, expected_start=None):
        _require(type(pid) is int and pid > 0 and hasattr(os, 'pidfd_open'))
        self.pid, self.fd = pid, None
        self.boot = _boot_id()
        before = _proc_start(pid)
        self.fd = os.pidfd_open(pid, 0)
        try:
            self.start = _proc_start(pid)
            _require(self.start == before and (expected_start is None or expected_start == self.start))
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        _require(self.fd is not None and not select.select([self.fd], [], [], 0)[0]
                 and _boot_id() == self.boot and _proc_start(self.pid) == self.start)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


from engineering_hub.executor_launch_profile import (  # re-exported; one parser for supervisor and verifiers
    DisjointDaemonPolicy, ExecutorLaunchMount, ExecutorLaunchProfile, LaunchProfileError, _HELD_HOST_IDS,
    _UNUSABLE_DAEMON_IDS, _covers, _covers_inside, _map_rows, _proc_map_rows)


@dataclass(frozen=True)
class _DaemonPin:
    socket_path: str
    pid: int
    start_ticks: int
    uid: int
    gid: int
    daemon_id: str
    api_version: str
    disjoint: InitVar[bool] = False

    def __post_init__(self, disjoint):
        _path(self.socket_path)
        _require(all(type(value) is int and value > 0 for value in (self.pid, self.start_ticks))
                 and all(type(value) is int and 0 <= value < 2**32 - 1 for value in (self.uid, self.gid))
                 and type(self.daemon_id) is str and re.fullmatch(r'[A-Za-z0-9:_.-]{1,128}', self.daemon_id)
                 and type(self.api_version) is str and re.fullmatch(r'1\.[0-9]{2,3}', self.api_version)
                 and int(self.api_version.split('.')[1]) >= 41)
        _require(type(disjoint) is bool)
        if disjoint:
            # A separate root-protected v3 enrollment pins this exact UID and GID.
            _require(self.uid not in _UNUSABLE_DAEMON_IDS and self.gid not in _UNUSABLE_DAEMON_IDS)
        else:
            _rootless_daemon_uid(self.uid)


def _rootless_daemon_uid(uid):
    # This deliberately narrow pilot enrolls only the allocated ehpilot daemon.
    _require(uid == 10001)


HELD_RANGES_SCHEMA = 'engineering-hub.held-id-ranges.v1'


def _host_ranges(value):
    _require(type(value) is list and 1 <= len(value) <= 16)
    ranges = []
    for item in value:
        _require(type(item) is list and len(item) == 2 and all(type(part) is int for part in item))
        start, length = item
        _require(start >= 0 and length > 0 and start + length <= 2**32)
        ranges.append((start, length))
    return tuple(ranges)


def _held_ranges(raw):
    """Operator-attested host ID ranges for the held TEST account.

    Root ownership protects the file's integrity after it is placed. It does not
    prove the ranges are complete or where they came from; the evidence digest is
    only checked for form. Overlap checks against it are only as good as that attestation.
    """
    data = _strict(raw, 65536)
    _require(type(data) is dict and set(data) == {'schema', 'uid_ranges', 'gid_ranges', 'evidence_sha256'}
             and data['schema'] == HELD_RANGES_SCHEMA
             and type(data['evidence_sha256']) is str and _HEX.fullmatch(data['evidence_sha256']))
    uids, gids = _host_ranges(data['uid_ranges']), _host_ranges(data['gid_ranges'])
    # A record that doesn't even cover the held account itself is not about it.
    _require(any(start <= 10001 < start + length for start, length in uids)
             and any(start <= 10001 < start + length for start, length in gids))
    return uids, gids


def _disjoint_from(rows, ranges):
    return not any(outside < start + length and start < outside + size
                   for _, outside, size in rows for start, length in ranges)


def _live_maps(policy, uid_map, gid_map):
    """Exact equality between kernel-reported maps and the profile-pinned policy."""
    try:
        rows = _proc_map_rows(uid_map), _proc_map_rows(gid_map)
    except LaunchProfileError:
        # Callers of the supervisor see only its own sanitized error type.
        raise LaunchSupervisorError() from None
    _require(rows == (policy.uid_map, policy.gid_map))


def _mapped_id(raw, identifier):
    _require(type(raw) is str and len(raw) <= 65536)
    rows = []
    for line in raw.splitlines():
        fields = line.split()
        _require(len(fields) == 3 and all(re.fullmatch(r'[0-9]{1,10}', value) for value in fields))
        inside, outside, length = map(int, fields)
        _require(length > 0 and inside + length <= 2**32 and outside + length <= 2**32)
        _require(all(inside + length <= old[0] or old[0] + old[2] <= inside for old in rows))
        rows.append((inside, outside, length))
    matches = [outside + identifier - inside for inside, outside, length in rows
               if inside <= identifier < inside + length]
    _require(len(matches) == 1)
    return matches[0]


def _kernel_profile(profile, status, uid_map, gid_map, mountinfo, consumer_uid, consumer_gid):
    """Validate caller-observed /proc data, not Engine's desired configuration."""
    identity = 10002 if profile.role == 'coordinator' else 10001
    _require(_mapped_id(uid_map, identity) == consumer_uid and _mapped_id(gid_map, identity) == consumer_gid)
    _require([int(value) for value in status['Uid'].split()] == [consumer_uid] * 4
             and [int(value) for value in status['Gid'].split()] == [consumer_gid] * 4
             and status.get('NoNewPrivs') == '1'
             and all(int(status[name], 16) == 0 for name in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')))
    actual_groups = [int(value) for value in status['Groups'].split()]
    _require(len(actual_groups) == len(set(actual_groups)))
    expected_groups = {_mapped_id(gid_map, int(value)) for value in profile.supplementary_groups}
    # Runtimes differ on including the primary GID in supplementary Groups.
    # No undeclared non-primary group is accepted; all declared groups are required.
    _require(expected_groups <= set(actual_groups) <= expected_groups | {consumer_gid})
    _require(type(mountinfo) is bytes and len(mountinfo) <= 1024**2)
    mounts = {}
    for line in mountinfo.decode('utf-8').splitlines():
        parts = line.split(' - ')
        _require(len(parts) == 2)
        left, right = parts[0].split(), parts[1].split()
        _require(len(left) >= 6 and len(right) >= 3)
        target = re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), left[4])
        # Stacked duplicate mountpoints make the effective mount ambiguous.
        _require(target not in mounts)
        mounts[target] = (set(left[5].split(',')), right[0])
    _require('/' in mounts and 'ro' in mounts['/'][0] and 'rw' not in mounts['/'][0])
    for mount in profile.mounts:
        _require(mount.target in mounts)
        flags = mounts[mount.target][0]
        wanted, denied = ('ro', 'rw') if mount.read_only else ('rw', 'ro')
        _require(wanted in flags and denied not in flags)
    _require('/tmp' in mounts and mounts['/tmp'][1] == 'tmpfs'
             and {'rw', 'nosuid', 'nodev', 'noexec'} <= mounts['/tmp'][0]
             and 'ro' not in mounts['/tmp'][0])


class _NoReconnect(http.client.HTTPConnection):
    def connect(self):
        raise LaunchSupervisorError()

    def close(self):
        # One-shot context owns the socket. HTTPConnection otherwise closes it
        # immediately for Connection:close responses, preventing deadline updates
        # while reading the response. Never reuse this connection object.
        pass


class _ArchiveReader:
    def __init__(self, response, peer, deadline):
        self.response, self.peer, self.deadline = response, peer, deadline
        self.total = 0

    def read(self, size):
        _require(type(size) is int and 0 <= size <= 1024**2)
        self.peer.settimeout(_remaining(self.deadline))
        value = self.response.read(min(size, 65536))
        self.total += len(value)
        _require(self.total <= 2 * 1024**3)
        _remaining(self.deadline)
        return value


class _UnixEngine:
    """Each request directly authenticates the pinned Linux daemon; no retries."""
    def __init__(self, pin):
        self.pin = pin
        self.process = _Process(pin.pid, expected_start=pin.start_ticks)
        try:
            self.namespace = _identity(os.stat('/proc/self/ns/pid'))
            _require(_identity(os.stat(f'/proc/{pin.pid}/ns/pid')) == self.namespace)
            self.socket_identity = _identity(os.lstat(pin.socket_path))
            _require(stat.S_ISSOCK(os.lstat(pin.socket_path).st_mode))
        except BaseException:
            self.close()
            raise

    def close(self):
        self.process.close()

    @contextmanager
    def request(self, method, route, deadline, *, body=None, statuses=(200,)):
        connection = _NoReconnect('docker', timeout=_remaining(deadline))
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        response = None
        try:
            self.process.check()
            _require(_identity(os.stat('/proc/self/ns/pid')) == self.namespace
                     and _identity(os.stat(f'/proc/{self.pin.pid}/ns/pid')) == self.namespace
                     and _identity(os.lstat(self.pin.socket_path)) == self.socket_identity)
            peer.settimeout(_remaining(deadline))
            peer.connect(self.pin.socket_path)
            observed = struct.unpack('=iII', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            _require(observed == (self.pin.pid, self.pin.uid, self.pin.gid)
                     and _identity(os.lstat(self.pin.socket_path)) == self.socket_identity)
            self.process.check()
            connection.sock = peer
            connection.request(method, '/v' + self.pin.api_version + route,
                body=None if body is None else _canonical(body),
                headers={'Host': 'docker', 'Accept-Encoding': 'identity', 'Content-Type': 'application/json',
                         'Connection': 'close'})
            response = connection.getresponse()
            headers = response.getheaders()
            _require(response.status in statuses
                     and len([value for key, value in headers if key.lower() == 'content-length']) <= 1
                     and response.getheader('Content-Encoding', 'identity').lower() == 'identity')
            if response.getheader('Transfer-Encoding') is not None:
                _require(response.getheader('Content-Length') is None
                         and response.getheader('Transfer-Encoding').lower() == 'chunked')
            self.process.check()
            yield response, peer
            _remaining(deadline)
            self.process.check()
        finally:
            if response is not None:
                response.close()
            peer.close()

    def json(self, method, route, deadline, *, body=None, statuses=(200,)):
        with self.request(method, route, deadline, body=body, statuses=statuses) as (response, peer):
            data = []
            total = 0
            while True:
                peer.settimeout(_remaining(deadline))
                part = response.read(min(65536, 1024**2 + 1 - total))
                if not part:
                    break
                total += len(part)
                _require(total <= 1024**2)
                data.append(part)
            raw = b''.join(data)
            _require(len(raw) <= 1024**2)
            if not raw:
                return response.status, None
            # Engine JSON need not use our canonical spelling, but duplicate
            # keys, nonfinite values and response reflection remain forbidden.
            data = json.loads(raw, object_pairs_hook=lambda pairs: _unique(pairs),
                              parse_constant=lambda _: _require(False))
            return response.status, data

    def image(self, image, deadline):
        _, info = self.json('GET', '/info', deadline)
        _require(type(info) is dict and info.get('ID') == self.pin.daemon_id
                 and info.get('OSType') == 'linux'
                 and type(info.get('SecurityOptions')) is list
                 and 'name=rootless' in info['SecurityOptions']
                 and info.get('Architecture') in ({'amd64', 'x86_64'} if image.platform.architecture == 'amd64'
                                                 else {'arm64', 'aarch64'}))
        _, inspected = self.json('GET', '/images/' + image.runnable_reference + '/json', deadline)
        with self.request('GET', '/images/' + image.runnable_reference + '/get', deadline) as (response, peer):
            config, defaults = _archive_image(_archive_metadata(_ArchiveReader(response, peer, deadline)), image)
        _require(type(inspected) is dict and inspected.get('Id') in {
            image.runnable_reference, image.platform_manifest_digest, image.image_config_id})
        _require(config == image.image_config and inspected.get('Config') == defaults)
        return defaults


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


_GATE_ISSUANCE_SCHEMA = ('CREATE TABLE gate_issuance (operation_id TEXT PRIMARY KEY, nonce TEXT UNIQUE NOT NULL, '
                         'gate_launch_id TEXT UNIQUE NOT NULL, supervisor_id TEXT UNIQUE NOT NULL, '
                         'decision TEXT NOT NULL, digest TEXT NOT NULL)')
_GATE_START_SCHEMA = ('CREATE TABLE gate_start (operation_id TEXT PRIMARY KEY, nonce TEXT UNIQUE NOT NULL, '
                      'gate_launch_id TEXT UNIQUE NOT NULL, supervisor_id TEXT UNIQUE NOT NULL, '
                      'enrollment TEXT NOT NULL, digest TEXT NOT NULL)')


class _Journal:
    """Root-private local durable single-launch/one-issuance reservation.

    Restart can inspect retained rows but never adopts an earlier launch. The
    lock is held until close; no row or lock file is deleted on uncertainty.
    """
    def __init__(self, path, *, create, gate_mode=False, start_mode=False):
        from engineering_hub.pilot_ownership import _local_filesystem
        _require(type(create) is bool and type(gate_mode) is bool and type(start_mode) is bool
                 and (gate_mode or not start_mode))
        self.gate_mode = gate_mode
        self.start_mode = start_mode
        self._gate_fresh = gate_mode and create
        self._start_fresh = start_mode and create
        self._start_confirmed = False
        self._gate_lock = threading.Lock()
        self.path, self.pid = _path(path), os.getpid()
        self.parent = self.lock = None
        try:
            self.parent = _root_parent(self.path, private=True)
            _local_filesystem(self.parent)
            lock_name = self.path.name + '.lock'
            self.lock = os.open(lock_name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC |
                                (os.O_CREAT | os.O_EXCL if create else 0), 0o600, dir_fd=self.parent)
            lock = os.fstat(self.lock)
            _require(stat.S_ISREG(lock.st_mode) and lock.st_uid == 0
                     and stat.S_IMODE(lock.st_mode) == 0o600 and lock.st_nlink == 1)
            self.lock_identity = _identity(lock)
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if create:
                fd = os.open(self.path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=self.parent)
                os.fsync(fd)
                os.close(fd)
                os.fsync(self.parent)
            info = os.stat(self.path.name, dir_fd=self.parent, follow_symlinks=False)
            _require(stat.S_ISREG(info.st_mode) and info.st_uid == 0
                     and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)
            self.identity = _identity(info)
            with self.transaction() as db:
                if create:
                    db.execute('CREATE TABLE launch (id TEXT PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL)')
                    db.execute('CREATE TABLE issuance (launch_id TEXT PRIMARY KEY, nonce TEXT UNIQUE NOT NULL, '
                               'state TEXT NOT NULL, receipt_digest TEXT)')
                    if gate_mode:
                        db.execute(_GATE_ISSUANCE_SCHEMA)
                    if start_mode:
                        db.execute(_GATE_START_SCHEMA)
                    db.execute('PRAGMA user_version=' + ('3' if start_mode else '2' if gate_mode else '1'))
                _require(db.execute('PRAGMA user_version').fetchone()[0] == (3 if start_mode else 2 if gate_mode else 1))
                tables = {'launch', 'issuance'} | ({'gate_issuance'} if gate_mode else set())
                if start_mode:
                    tables.add('gate_start')
                _require({row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} == tables)
                if gate_mode:
                    _require(db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='gate_issuance'").fetchone()
                             == (_GATE_ISSUANCE_SCHEMA,))
                if start_mode:
                    _require(db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='gate_start'").fetchone()
                             == (_GATE_START_SCHEMA,))
        except BaseException:
            self.close()
            raise

    def _check(self):
        _require(self.pid == os.getpid() and self.parent is not None and self.lock is not None
                 and _identity(os.stat(self.path.parent, follow_symlinks=False)) == _identity(os.fstat(self.parent))
                 and self.path.parent.resolve(strict=True) == self.path.parent
                 and _identity(os.stat(self.path.name, dir_fd=self.parent, follow_symlinks=False)) == self.identity
                 and _identity(os.stat(self.path.name + '.lock', dir_fd=self.parent, follow_symlinks=False)) == self.lock_identity)

    @contextmanager
    def transaction(self):
        self._check()
        db = sqlite3.connect(str(self.path), timeout=1, isolation_level=None)
        try:
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            self._check()
            db.commit()
            os.fsync(self.parent)
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def reserve(self, identity, data):
        with self.transaction() as db:
            _require(db.execute('SELECT count(*) FROM launch').fetchone()[0] == 0)
            db.execute('INSERT INTO launch VALUES (?,?,?)', (identity, 'create_reserved', _canonical(data).decode()))

    def state(self, identity, state, data):
        with self.transaction() as db:
            _require(db.execute('UPDATE launch SET state=?,data=? WHERE id=?',
                (state, _canonical(data).decode(), identity)).rowcount == 1)

    def reserve_issuance(self, identity, nonce):
        with self.transaction() as db:
            _require(db.execute('SELECT state FROM launch WHERE id=?', (identity,)).fetchone() == ('running',))
            db.execute('INSERT INTO issuance VALUES (?,?,?,NULL)', (identity, nonce, 'reserved'))

    def issuance_state(self, identity, state, digest):
        with self.transaction() as db:
            _require(db.execute('UPDATE issuance SET state=?,receipt_digest=? WHERE launch_id=?',
                                (state, digest, identity)).rowcount == 1)

    def reserve_gate_start(self, supervisor_id, supervisor_record, expected_without_nonce,
                           bootstrap_digest, payload, digest):
        """Durably reserve one start before sending; no cross-ledger atomicity."""
        from .mount_gate_start_enrollment import PROTOCOL, AUDIENCE, _identity
        from .mount_gate_protocol import IDENTITY_FIELDS
        self._check()
        with self._gate_lock:
            try:
                _require(self.start_mode and self._start_fresh and type(supervisor_id) is str
                         and type(supervisor_record) is dict and type(expected_without_nonce) is dict
                         and set(expected_without_nonce) == IDENTITY_FIELDS - {'nonce'}
                         and type(payload) is dict and set(payload) == {'protocol', 'audience', 'identity',
                             'bootstrap_digest', 'issued_at_ms', 'expires_at_ms'}
                         and payload['protocol'] == PROTOCOL and payload['audience'] == AUDIENCE
                         and type(bootstrap_digest) is str and _SHA.fullmatch(bootstrap_digest)
                         and payload['bootstrap_digest'] == bootstrap_digest
                         and type(digest) is str and _HEX.fullmatch(digest))
                pins = payload['identity']; _identity(pins)
                _require(all(type(pins[k]) is type(expected_without_nonce[k]) and pins[k] == expected_without_nonce[k]
                             for k in expected_without_nonce))
                issued, expires = payload['issued_at_ms'], payload['expires_at_ms']
                _require(type(issued) is int and type(expires) is int and issued > 0
                         and 0 < expires - issued <= 5000)
                with self.transaction() as db:
                    row = db.execute('SELECT state,data FROM launch WHERE id=?', (supervisor_id,)).fetchone()
                    _require(row == ('running', _canonical(supervisor_record).decode()))
                    db.execute('INSERT INTO gate_start VALUES (?,?,?,?,?,?)',
                        (pins['operation_id'], pins['nonce'], pins['launch_id'], supervisor_id,
                         _canonical(payload).decode(), digest))
            except BaseException:
                self._start_fresh = False
                self._gate_fresh = False
                raise
            self._start_fresh = False
            self._start_confirmed = True
            return True

    def reserve_gate_issuance(self, supervisor_id, supervisor_record, expected, decision, digest):
        """One durable reservation in this journal; external authorities stay external.

        Trusted composition must hold the supervisor lifecycle lock and validate
        live application process, fence, custodian and independent enrollment.
        This transaction cannot atomically inspect the separate adapter ledger.
        A raised error, including after commit, is uncertain and never retryable.
        """
        self._check()  # PID fence precedes a possibly inherited lock.
        with self._gate_lock:
            return self._reserve_gate_issuance(supervisor_id, supervisor_record, expected, decision, digest)

    def _reserve_gate_issuance(self, supervisor_id, supervisor_record, expected, decision, digest):
        from .mount_gate_protocol import IDENTITY_FIELDS, _payload
        _require(self.gate_mode and self._gate_fresh and type(supervisor_id) is str
                 and type(supervisor_record) is dict and type(expected) is dict
                 and set(expected) == IDENTITY_FIELDS and type(digest) is str
                 and re.fullmatch(r'[0-9a-f]{64}', digest) is not None)
        _payload(decision)
        _require(all(type(expected[k]) is type(decision[k]) and expected[k] == decision[k]
                     for k in IDENTITY_FIELDS))
        encoded = _canonical(decision).decode()
        try:
            with self.transaction() as db:
                row = db.execute('SELECT state,data FROM launch WHERE id=?', (supervisor_id,)).fetchone()
                _require(row == ('running', _canonical(supervisor_record).decode()))
                if self.start_mode:
                    _require(self._start_confirmed)
                    started = db.execute('SELECT enrollment FROM gate_start WHERE supervisor_id=?', (supervisor_id,)).fetchone()
                    _require(started is not None and json.loads(started[0])['identity'] == expected)
                db.execute('INSERT INTO gate_issuance VALUES (?,?,?,?,?,?)',
                           (decision['operation_id'], decision['nonce'], decision['launch_id'], supervisor_id, encoded, digest))
        except BaseException:
            # Even an exception after SQLite commit is ambiguous. Permanently
            # close this in-process issuance path until manual reconciliation.
            self._gate_fresh = False
            raise
        return True

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None
        if self.parent is not None:
            os.close(self.parent)
            self.parent = None


class LaunchSupervisor:
    """Explicit ROOT operator API: one create/start, exact init peer, one receipt.

    Construct with existing independent enrollment/key and a private durable
    journal. launch() creates the exact pinned image once; serve_one_receipt()
    synchronously handles one bounded connection. No service is installed or
    background thread created. close() closes descriptors ONLY: it does not claim
    container quiescence. graceful_stop() is a separate explicit SIGTERM action.
    No automatic name adoption, POST retry, Docker stop/kill timeout, remove,
    cleanup of generations, key creation or restart recovery is implemented.
    """
    def __init__(self, *, enrollment_file, signing_key_file, journal_file, create_journal,
                 held_ranges_file=None, gate_start_mode=False):
        self._pid = os.getpid()
        self._held_path = self._held_identity = None
        self._mutex = threading.Lock()
        self._gate_start_thread = None
        self._gate_listener = self._gate_parent = None
        self._gate_directory = self._gate_socket_identity = self._gate_enrollment = None
        self._gate_accepted_channel = self._gate_accepted_peer = None
        self._gate_accept_used = False
        self._journal = self._engine = self._consumer = self._listener = None
        self._socket_parent = None
        self._socket_identity = None
        self._launch_id = self._container_id = None
        self._pair_reservation = None
        self._closed = False
        self._issuance_open = False
        self._receipt_served = False
        self._approval_expires_at_ms = None
        self._held = False
        self._source_pins = {}
        try:
            _require(type(gate_start_mode) is bool and sys.platform == 'linux' and os.geteuid() == os.getuid() == 0
                     and hasattr(socket, 'SO_PEERCRED') and hasattr(os, 'pidfd_open'))
            self._enrollment_path, self._key_path = _path(enrollment_file), _path(signing_key_file)
            raw, self._enrollment_identity = _root_file(self._enrollment_path, 1024**2)
            key, self._key_identity = _root_file(self._key_path, 32)
            _require(len(key) == 32)
            self._signer = Ed25519PrivateKey.from_private_bytes(key)
            data = _strict(raw, 1024**2)
            _require(type(data) is dict and set(data) == {'schema', 'release', 'image', 'profile', 'daemon',
                'socket_path', 'consumer_host_uid', 'consumer_host_gid', 'receipt_lifetime_seconds',
                'request_timeout_ms', 'launch_timeout_ms', 'configuration_raw_digest'})
            _require(data['schema'] in ('executor_launch_supervisor_v2', 'executor_launch_supervisor_v3'))
            self._configuration_raw_digest = data['configuration_raw_digest']
            _require(type(self._configuration_raw_digest) is str
                     and _SHA.fullmatch(self._configuration_raw_digest))
            release = dict(data['release'])
            _require(type(release.get('public_key')) is str and _HEX.fullmatch(release['public_key']))
            release['public_key'] = bytes.fromhex(release['public_key'])
            release['platform'] = Platform.model_validate(release['platform'])
            self.release = ExecutorReleaseEnrollment(**release)
            _require(self.release.public_key == self._signer.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw))
            self.profile = ExecutorLaunchProfile.model_validate(data['profile'])
            _require(self.profile.digest() == self.release.launch_profile_digest)
            # Broker transport and independent enrollment are not implemented,
            # nor are coordinator pair launch and inherited-channel handoff.
            # Refuse before journal creation, socket binding or Engine access.
            _require(self.profile.broker_mode is None and self.profile.role is None
                     and self.profile.command_channel is None)
            image = dict(data['image'])
            _require(set(image) == set(VerifiedImage.__dataclass_fields__))
            image['platform'] = Platform.model_validate(image['platform'])
            self.image = VerifiedImage(**image)
            _require(self.image.runnable_reference == self.release.runnable_digest
                     and self.image.platform_manifest_digest == self.release.platform_manifest_digest
                     and self.image.image_config_id == self.release.image_config_id
                     and self.image.platform == self.release.platform
                     and self.image.source_commit == self.release.implementation_commit)
            _require(type(data['daemon']) is dict and 'disjoint' not in data['daemon'])
            disjoint = data['schema'] == 'executor_launch_supervisor_v3'
            self._daemon_pin = _DaemonPin(**data['daemon'], disjoint=disjoint)
            self._socket_path = _path(data['socket_path'])
            self._consumer_uid, self._consumer_gid = data['consumer_host_uid'], data['consumer_host_gid']
            _require(all(type(value) is int and 0 < value < 2**32 - 1
                         for value in (self._consumer_uid, self._consumer_gid)))
            policy = self.profile.daemon_policy
            # The version and the signed profile must agree: legacy never carries a
            # policy or held evidence; v3 needs both and can't run without them.
            _require((policy is not None) is disjoint and (held_ranges_file is not None) is disjoint)
            if disjoint:
                self._held_path = _path(held_ranges_file)
                held_raw, self._held_identity = _root_file(self._held_path, 65536)
                # Only the exact attestation the signed profile names may be compared against.
                _require('sha256:' + hashlib.sha256(held_raw).hexdigest() == policy.held_ranges_digest)
                held_uids, held_gids = _held_ranges(held_raw)
                _require(self._daemon_pin.uid == policy.daemon_uid and self._daemon_pin.gid == policy.daemon_gid
                         and self._daemon_pin.daemon_id == policy.engine_id
                         and _disjoint_from(policy.uid_map, held_uids) and _disjoint_from(policy.gid_map, held_gids))
                # Signed role selects the namespace account whose host identity is pinned.
                identity = 10002 if self.profile.role == 'coordinator' else 10001
                uid_text = ''.join(f'{a} {b} {c}\n' for a, b, c in policy.uid_map)
                gid_text = ''.join(f'{a} {b} {c}\n' for a, b, c in policy.gid_map)
                _require(_mapped_id(uid_text, identity) == self._consumer_uid
                         and _mapped_id(gid_text, identity) == self._consumer_gid)
                _require(len({self._held_path, self._enrollment_path, self._key_path, _path(journal_file)}) == 4)
            else:
                self._held_path = self._held_identity = None
            self._ttl, self._request_ms, self._launch_ms = (data['receipt_lifetime_seconds'],
                data['request_timeout_ms'], data['launch_timeout_ms'])
            _require(type(self._ttl) is int and 1 <= self._ttl <= 3600
                     and type(self._request_ms) is int and 100 <= self._request_ms <= 10000
                     and type(self._launch_ms) is int and 100 <= self._launch_ms <= 120000)
            self._mounts = {item.role: item for item in self.profile.mounts}
            _require(self._mounts['issuer_socket'].source == str(self._socket_path)
                     and self._mounts['issuer_socket'].gid == self._consumer_gid
                     and self._mounts['config'].uid == self._consumer_uid
                     and self._mounts['config'].gid == self._consumer_gid
                     and self._mounts['engine_socket'].source == self._daemon_pin.socket_path)
            protected = (self._key_path.parent, self._enrollment_path.parent, _path(journal_file).parent)
            for mount in self.profile.mounts:
                source = Path(mount.source)
                _require(not any(source == item or source in item.parents or item in source.parents for item in protected))
            _require(self._key_path != self._enrollment_path and self._key_path != _path(journal_file)
                     and self._enrollment_path != _path(journal_file))
            self._journal = _Journal(journal_file, create=create_journal,
                                     gate_mode=gate_start_mode, start_mode=gate_start_mode)
            self._engine = _UnixEngine(self._daemon_pin)
        except BaseException:
            self.close()
            raise LaunchSupervisorError() from None

    def _guard(self, *, allow_held=False):
        _require(self._pid == os.getpid() and not self._closed and (allow_held or not self._held))
        _require(_root_file(self._enrollment_path, 1024**2)[1] == self._enrollment_identity
                 and _root_file(self._key_path, 32)[1] == self._key_identity)
        if self._held_path is not None:
            _require(_root_file(self._held_path, 65536)[1] == self._held_identity)

    def _check_daemon_maps(self):
        """v3 only: the pinned daemon's live kernel maps equal the signed policy."""
        policy = self.profile.daemon_policy
        if policy is None:
            return
        self._engine.process.check()
        _live_maps(policy, Path(f'/proc/{self._daemon_pin.pid}/uid_map').read_text(),
                   Path(f'/proc/{self._daemon_pin.pid}/gid_map').read_text())
        self._engine.process.check()

    def _hold(self):
        self._held = True
        self._issuance_open = False
        self._hold_persisted = False
        if self._launch_id is not None:
            try:
                self._journal.state(self._launch_id, 'held', self._record())
                self._hold_persisted = True
            except BaseException:
                # Original durable intent remains authoritative. A damaged
                # journal cannot certify that our held update was persisted.
                # Never leak its exception or reopen/retry it automatically.
                pass

    def _record(self):
        return {'container_id': self._container_id, 'manifest': self.release.launch_profile_digest,
                'approval_expires_at_ms': getattr(self, '_approval_expires_at_ms', None),
                'engine_id': getattr(getattr(self, '_daemon_pin', None), 'daemon_id', None),
                'engine_pid': getattr(getattr(self, '_daemon_pin', None), 'pid', None),
                'engine_start_ticks': getattr(getattr(self, '_daemon_pin', None), 'start_ticks', None),
                'boot_id': None if self._consumer is None else self._consumer.boot,
                'pid': None if self._consumer is None else self._consumer.pid,
                'start_ticks': None if self._consumer is None else self._consumer.start}

    def _pin_sources(self):
        for role, mount in self._mounts.items():
            path = _path(mount.source)
            _require(path.resolve(strict=True) == path)
            info = path.lstat()
            valid_kind = (stat.S_ISSOCK(info.st_mode) if mount.kind == 'socket' else
                          stat.S_ISREG(info.st_mode) if mount.kind == 'file' else stat.S_ISDIR(info.st_mode))
            _require(valid_kind and info.st_uid == mount.uid and info.st_gid == mount.gid
                     and stat.S_IMODE(info.st_mode) == mount.mode and not stat.S_ISLNK(info.st_mode))
            if mount.kind != 'directory':
                _require(info.st_nlink == 1)
            if mount.kind == 'file':
                _require(info.st_size <= 65536)
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
                try:
                    raw = os.read(fd, 65537)
                    _require(len(raw) == info.st_size and _identity(os.fstat(fd)) == _identity(info))
                    if self.profile.role == 'coordinator':
                        commitment = coordinator_configuration_commitment(raw)
                        _require(commitment.raw_digest == self._configuration_raw_digest
                                 and commitment.content_digest == mount.content_digest)
                    else:
                        commitment = configuration_commitment(raw)
                        self._runner_configuration(raw)
                        _require(commitment.raw_digest == self._configuration_raw_digest
                                 and commitment.content_digest == mount.content_digest
                                 and commitment.release == self.release
                                 and commitment.release.launch_profile_digest == self.profile.digest())
                finally:
                    os.close(fd)
            if role in self._source_pins:
                _require(self._source_pins[role] == _identity(info))
            else:
                self._source_pins[role] = _identity(info)

    def _bind_issuer(self):
        self._socket_parent = _root_parent(self._socket_path, private=False)
        try:
            os.stat(self._socket_path.name, dir_fd=self._socket_parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise LaunchSupervisorError()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.set_inheritable(False)
        pinned = f'/proc/self/fd/{self._socket_parent}/{self._socket_path.name}'
        _require(len(pinned.encode()) <= 107)
        self._listener.bind(pinned)
        info = os.stat(self._socket_path.name, dir_fd=self._socket_parent, follow_symlinks=False)
        self._socket_identity = _identity(info)
        os.chown(pinned, 0, self._consumer_gid, follow_symlinks=False)
        os.chmod(pinned, 0o660, follow_symlinks=False)
        self._listener.listen(1)

    def _create_body(self):
        p = self.profile
        return {'Image': self.image.runnable_reference, 'User': p.user,
            'Entrypoint': list(p.entrypoint), 'Cmd': list(p.command), 'Env': list(p.environment),
            'WorkingDir': '/', 'Hostname': '', 'Domainname': '', 'AttachStdin': False,
            'AttachStdout': False, 'AttachStderr': False, 'OpenStdin': False, 'Tty': False,
            'HostConfig': {'ReadonlyRootfs': True, 'Privileged': False, 'CapDrop': ['ALL'],
                'CapAdd': [], 'SecurityOpt': ['no-new-privileges:true'], 'Init': False,
                'PidMode': '', 'IpcMode': 'private', 'CgroupnsMode': 'private', 'NetworkMode': 'none',
                'PortBindings': {}, 'PublishAllPorts': False, 'Memory': p.memory_bytes,
                'MemorySwap': p.memory_bytes, 'NanoCpus': p.cpu_millicores * 1_000_000,
                'PidsLimit': p.pids_limit, 'GroupAdd': [str(value) for value in p.supplementary_groups],
                'Tmpfs': {'/tmp': f'rw,noexec,nosuid,nodev,size={p.tmpfs_bytes},mode=1777'},
                'Mounts': [{'Type': 'bind', 'Source': item.source, 'Target': item.target,
                            'ReadOnly': item.read_only, 'BindOptions': {'Propagation': 'rprivate'}} for item in p.mounts],
                'RestartPolicy': {'Name': 'no', 'MaximumRetryCount': 0}, 'AutoRemove': False,
                'Devices': [], 'DeviceRequests': [], 'ExtraHosts': [], 'Links': [], 'VolumesFrom': []}}

    def _inspect(self, deadline, *, running):
        _, info = self._engine.json('GET', '/containers/' + self._container_id + '/json', deadline)
        _require(type(info) is dict and info.get('Id') == self._container_id
                 and info.get('Image') in {self.image.image_config_id,
                     self.image.platform_manifest_digest, self.image.runnable_reference})
        expected = self._create_body()
        config, host, state = info.get('Config'), info.get('HostConfig'), info.get('State')
        _require(type(config) is dict and type(host) is dict and type(state) is dict)
        _require(config.get('Image') == self.image.runnable_reference)
        for key in ('User', 'Entrypoint', 'Cmd', 'Env', 'WorkingDir', 'AttachStdin', 'AttachStdout',
                    'AttachStderr', 'OpenStdin', 'Tty'):
            _require(config.get(key) == expected[key])
        _require(config.get('Volumes') in (None, {}) and not config.get('Healthcheck'))
        for key, value in expected['HostConfig'].items():
            if key == 'Mounts':
                continue
            actual = host.get(key)
            if key in {'CapAdd', 'Devices', 'DeviceRequests', 'ExtraHosts', 'Links', 'VolumesFrom'}:
                _require(actual in (None, []))
            else:
                _require(actual == value)
        actual_mounts = info.get('Mounts')
        _require(type(actual_mounts) is list and len(actual_mounts) == len(self.profile.mounts))
        observed = {}
        for item in actual_mounts:
            _require(type(item) is dict and item.get('Destination') not in observed)
            observed[item.get('Destination')] = item
        for mount in self.profile.mounts:
            actual = observed.pop(mount.target, None)
            _require(type(actual) is dict and actual.get('Type') == 'bind' and actual.get('Source') == mount.source
                     and actual.get('RW') is (not mount.read_only) and actual.get('Propagation') == 'rprivate')
        # Docker29 inspect.Mounts lists binds but excludes HostConfig.Tmpfs.
        # Tmpfs configuration is checked above; actual mount flags below.
        _require(not observed)
        _require(not state.get('Restarting') and not state.get('Paused') and not state.get('Dead'))
        _require(type(state.get('Running')) is bool)
        if running is not None:
            _require(state['Running'] is running)
        if state['Running']:
            _require(state.get('Status') == 'running' and type(state.get('Pid')) is int and state['Pid'] > 0
                     and info.get('Path') == self.profile.entrypoint[0]
                     and info.get('Args') == list((*self.profile.entrypoint[1:], *self.profile.command)))
        return state

    def _wait_stopped(self, deadline):
        """Reconcile Engine accounting after kernel exit, without another effect.

        A readable pidfd can precede Docker's terminal state. Only the exact
        validated profile still reporting our original PID is worth waiting
        for. Changed profiles, malformed states and GET failures fail closed.
        """
        while True:
            _remaining(deadline)
            self._guard(allow_held=True)
            state = self._inspect(deadline, running=None)
            _remaining(deadline)
            if not state['Running']:
                _require(state.get('Status') == 'exited'
                         and type(state.get('Pid')) is int and state['Pid'] == 0)
                return
            _require(state['Pid'] == self._consumer.pid)
            time.sleep(min(0.05, _remaining(deadline)))

    def launch(self, *, pair_reservation=None):
        """One bounded real create/start. Any uncertain effect stays held forever."""
        _require(self._pid == os.getpid())
        with self._mutex:
            try:
                self._guard()
                _require(self._launch_id is None)
                _require(pair_reservation is getattr(self, '_pair_reservation', None))
                deadline = time.monotonic() + self._launch_ms / 1000
                # Before any socket bind or Engine request: wrong live maps mean no effect at all.
                self._check_daemon_maps()
                if pair_reservation is None:
                    self._bind_issuer()
                self._pin_sources()
                defaults = self._engine.image(self.image, deadline)
                _require(not defaults.get('Volumes') and not defaults.get('Healthcheck') and not defaults.get('OnBuild')
                         and tuple(defaults.get('Entrypoint') or ()) == self.profile.entrypoint
                         and tuple(defaults.get('Cmd') or ()) == self.profile.command
                         and tuple(defaults.get('Env') or ()) == self.profile.environment)
                launch_id = (str(uuid4()) if pair_reservation is None else
                             pair_reservation._claim(self))
                self._origin = time.monotonic()
                wall = time.time()
                _require(type(self._origin) in (int, float) and math.isfinite(self._origin)
                         and type(wall) in (int, float) and math.isfinite(wall) and wall > 0)
                self._expires = self._origin + self._ttl
                self._approval_expires_at_ms = int(wall * 1000) + self._ttl * 1000
                self._journal.reserve(launch_id, {'profile_digest': self.profile.digest(),
                    'approval_expires_at_ms': self._approval_expires_at_ms,
                    'image': self.image.runnable_reference,
                    'request_digest': 'sha256:' + hashlib.sha256(_canonical(self._create_body())).hexdigest()})
                self._launch_id = launch_id
                if pair_reservation is not None:
                    pair_reservation._check_launch(self)
                _, created = self._engine.json('POST', '/containers/create?name=eh-executor-' + launch_id,
                    deadline, body=self._create_body(), statuses=(201,))
                _require(type(created) is dict and set(created) <= {'Id', 'Warnings'}
                         and type(created.get('Id')) is str and _HEX.fullmatch(created['Id'])
                         and created.get('Warnings', []) in (None, []))
                self._container_id = created['Id']
                self._journal.state(launch_id, 'created', self._record())
                self._inspect(deadline, running=False)
                self._journal.state(launch_id, 'start_reserved', self._record())
                self._engine.json('POST', '/containers/' + self._container_id + '/start', deadline, statuses=(204,))
                state = self._inspect(deadline, running=True)
                self._consumer = _Process(state['Pid'])
                self._validate_live(deadline)
                self._journal.state(launch_id, 'running', self._record())
                self._issuance_open = True
                return self._container_id
            except BaseException:
                self._hold()
                raise LaunchSupervisorError() from None

    def _validate_live(self, deadline, *, allow_held=False):
        self._guard(allow_held=allow_held)
        _require(self._consumer is not None)
        self._consumer.check()
        _require(_identity(os.stat(f'/proc/{self._consumer.pid}/ns/pid')) != self._engine.namespace)
        state = self._inspect(deadline, running=True)
        _require(state['Pid'] == self._consumer.pid)
        self._pin_sources()
        status = {}
        for line in Path(f'/proc/{self._consumer.pid}/status').read_text().splitlines():
            if ':' in line:
                key, value = line.split(':', 1)
                status[key] = value.strip()
        consumer_uid_map = Path(f'/proc/{self._consumer.pid}/uid_map').read_text()
        consumer_gid_map = Path(f'/proc/{self._consumer.pid}/gid_map').read_text()
        _kernel_profile(self.profile, status, consumer_uid_map, consumer_gid_map,
                        Path(f'/proc/{self._consumer.pid}/mountinfo').read_bytes(),
                        self._consumer_uid, self._consumer_gid)
        if self.profile.daemon_policy is not None:
            # Rootless containers share the daemon's user namespace: both must match the policy.
            _live_maps(self.profile.daemon_policy, consumer_uid_map, consumer_gid_map)
            self._check_daemon_maps()
        for namespace in ('ipc', 'cgroup', 'net'):
            _require(_identity(os.stat(f'/proc/{self._consumer.pid}/ns/{namespace}'))
                     != _identity(os.stat('/proc/self/ns/' + namespace)))
        # Config readback is not proof that rootless cgroup limits took effect.
        groups = Path(f'/proc/{self._consumer.pid}/cgroup').read_text().splitlines()
        _require(len(groups) == 1 and groups[0].startswith('0::/'))
        relative = groups[0][3:]
        _require(os.path.normpath(relative) == relative and '..' not in Path(relative).parts)
        cgroup = Path('/sys/fs/cgroup') / relative.lstrip('/')
        _require(cgroup.resolve(strict=True) == cgroup)
        def value(name):
            raw = (cgroup / name).read_bytes()
            _require(len(raw) <= 4096)
            return raw.decode('ascii').strip()
        quota, period = value('cpu.max').split()
        _require(int(value('memory.max')) == self.profile.memory_bytes
                 and int(value('memory.swap.max')) == 0
                 and int(value('pids.max')) == self.profile.pids_limit
                 and quota != 'max' and int(period) > 0
                 and int(quota) * 1000 == self.profile.cpu_millicores * int(period)
                 and str(self._consumer.pid) in value('cgroup.procs').splitlines())
        arguments = Path(f'/proc/{self._consumer.pid}/cmdline').read_bytes().split(b'\0')
        _require(arguments[-1:] == [b''] and [part.decode('utf-8') for part in arguments[:-1]]
                 == [*self.profile.entrypoint, *self.profile.command])
        root = Path(f'/proc/{self._consumer.pid}/root')
        for mount in self.profile.mounts:
            observed = os.stat(str(root) + mount.target, follow_symlinks=False)
            _require(_identity(observed) == self._source_pins[mount.role])
        runner = str(root) + self.profile.runner_file
        fd = os.open(runner, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            _require(stat.S_ISREG(info.st_mode) and info.st_size <= 1024**2 and not info.st_mode & 0o022)
            raw = os.read(fd, 1024**2 + 1)
            _require(len(raw) == info.st_size and 'sha256:' + hashlib.sha256(raw).hexdigest() == self.profile.runner_digest)
        finally:
            os.close(fd)
        self._consumer.check()
        _remaining(deadline)

    @staticmethod
    def _receive(peer, size, deadline):
        parts = []
        while size:
            peer.settimeout(_remaining(deadline))
            part = peer.recv(size)
            _require(bool(part))
            parts.append(part)
            size -= len(part)
        return b''.join(parts)

    def wait_for_receipt_request(self, *, timeout_ms, stop_requested=None):
        """Bound cold-start readiness separately from the unchanged exchange.

        This observes only our retained listener and consumer pidfd. Readiness
        is not authenticated identity or permission to issue; serve_one_receipt
        still performs every existing guard and uses its original whole-exchange
        deadline. No Engine operation or key read occurs here. A trusted optional
        flag-reader runs outside the mutex; True cancels without an effect so
        the caller can explicitly request owned graceful stop. Uncertainty and
        timeout hold the original launch, never retry or adopt a consumer.
        """
        _require(self._pid == os.getpid())
        _require(type(timeout_ms) is int and 1 <= timeout_ms <= 60000
                 and (stop_requested is None or callable(stop_requested)))
        deadline = time.monotonic() + timeout_ms / 1000
        try:
            while True:
                cancelled = False if stop_requested is None else stop_requested()
                _require(type(cancelled) is bool)
                with self._mutex:
                    _require(not self._closed and not self._held and self._issuance_open
                             and self._consumer is not None
                             and type(self._listener) is socket.socket
                             and self._listener.fileno() >= 0)
                    self._consumer.check()
                    if cancelled:
                        return False
                    deadline = min(deadline, self._expires)
                    _remaining(deadline)
                    ready, _, _ = select.select([self._listener, self._consumer.fd], [], [],
                        min(0.1, _remaining(deadline)))
                    _remaining(deadline)
                    _require(self._consumer.fd not in ready)
                    if self._listener in ready:
                        return True
        except BaseException:
            with self._mutex:
                self._hold()
            raise LaunchSupervisorError() from None

    def serve_one_receipt(self):
        """One direct init-peer exchange; caller explicitly chooses to serve it."""
        _require(self._pid == os.getpid())
        with self._mutex:
            peer = None
            reserved = False
            try:
                self._guard()
                _require(self._issuance_open and time.monotonic() < self._expires)
                deadline = min(self._expires, time.monotonic() + self._request_ms / 1000)
                self._listener.settimeout(_remaining(deadline))
                peer, _ = self._listener.accept()
                credentials = struct.unpack('=iII', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                _require(credentials == (self._consumer.pid, self._consumer_uid, self._consumer_gid))
                self._validate_live(deadline)
                size = struct.unpack('!I', self._receive(peer, 4, deadline))[0]
                _require(0 < size <= MAX_REQUEST_BYTES)
                request = _strict(self._receive(peer, size, deadline), MAX_REQUEST_BYTES)
                _require(type(request) is dict and set(request) == {'protocol', 'nonce'}
                         and request['protocol'] == REQUEST_PROTOCOL and type(request['nonce']) is str
                         and _HEX.fullmatch(request['nonce']))
                peer.settimeout(_remaining(deadline))
                _require(peer.recv(1) == b'')  # One frame plus explicit SHUT_WR, no trailing data.
                self._validate_live(deadline)
                self._journal.reserve_issuance(self._launch_id, request['nonce'])
                reserved = True
                self._issuance_open = False
                self._validate_live(deadline)
                now = int(time.time() * 1000)
                remaining = int((self._expires - time.monotonic()) * 1000)
                _require(remaining > 0)
                payload = {'protocol': PROTOCOL, 'audience': AUDIENCE, 'nonce': request['nonce'],
                    'receipt_id': str(uuid4()), 'launch_id': self._launch_id, 'issuer_id': self.release.issuer_id,
                    'boot_id': self._consumer.boot, 'authenticated_host_pid': self._consumer.pid,
                    'process_start_ticks': self._consumer.start, 'container_id': self._container_id,
                    'runnable_digest': self.release.runnable_digest,
                    'platform_manifest_digest': self.release.platform_manifest_digest,
                    'image_config_id': self.release.image_config_id,
                    'platform': self.release.platform.model_dump(mode='json'),
                    'implementation_commit': self.release.implementation_commit,
                    'launch_profile_digest': self.release.launch_profile_digest,
                    'issued_at_ms': now, 'expires_at_ms': now + remaining}
                signature = base64.urlsafe_b64encode(self._signer.sign(SIGNATURE_DOMAIN + _canonical(payload))).rstrip(b'=').decode()
                raw = _canonical({'payload': payload, 'signature': signature})
                _require(len(raw) <= MAX_RECEIPT_BYTES)
                self._validate_live(deadline)
                peer.settimeout(_remaining(deadline))
                peer.sendall(struct.pack('!I', len(raw)) + raw)
                peer.shutdown(socket.SHUT_WR)
                _remaining(deadline)
                self._journal.issuance_state(self._launch_id, 'sent', hashlib.sha256(raw).hexdigest())
                self._receipt_served = True
                return payload['receipt_id']
            except BaseException:
                if reserved:
                    try:
                        self._journal.issuance_state(self._launch_id, 'held', None)
                    except BaseException:
                        pass
                self._hold()
                raise LaunchSupervisorError() from None
            finally:
                if peer is not None:
                    peer.close()

    def _runner_configuration(self, raw):
        from .installed_runner import parse_configuration
        parsed = parse_configuration(raw)
        # Paired runners consume an inherited channel. A legacy signed launch
        # profile cannot authorize that different entrypoint behavior.
        _require((parsed.pair is not None) ==
                 (self.profile.command_channel == 'inherited-seqpacket-v1'))
        return parsed

    def _handoff_manifest_digest(self):
        """Derive only from the already pinned canonical runner configuration."""
        mount = self._mounts['config']
        path = _path(mount.source)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            named = os.lstat(path)
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                     and _identity(info) == _identity(named)
                     and _identity(info) == self._source_pins['config']
                     and 0 < info.st_size <= 65536)
            raw = bytearray()
            while len(raw) <= 65536:
                chunk = os.read(fd, min(65537 - len(raw), 65536))
                if not chunk:
                    break
                raw.extend(chunk)
            _require(len(raw) == info.st_size and _identity(os.fstat(fd)) == _identity(info)
                     and _identity(os.lstat(path)) == _identity(info))
            raw = bytes(raw)
            if self.profile.role == 'coordinator':
                commitment = coordinator_configuration_commitment(raw)
                _require(commitment.raw_digest == self._configuration_raw_digest
                         and commitment.content_digest == mount.content_digest)
                return commitment.manifest_digest
            commitment = configuration_commitment(raw)
            _require(commitment.raw_digest == self._configuration_raw_digest
                     and commitment.content_digest == mount.content_digest
                     and commitment.release == self.release)
            return self._runner_configuration(raw).manifest.digest()
        finally:
            os.close(fd)

    def serve_one_executor_handoff(self, channel):
        """Issue one measured-executor endpoint receipt; pair launch remains disabled."""
        return self._serve_one_role_handoff(channel, 'executor')

    def prepare_gate_socket(self, *, manifest, image, enrollment_file, timeout_seconds):
        """Bind a single protected v3 gate endpoint before container creation.

        This retains socket and parent descriptors until supervisor close; close
        leaves the pathname as evidence. The caller must pass the enrolled source
        directory through an independently authorized Docker profile. This
        method does not authorize that mount or enable an application launch.
        """
        from .application_gate_enrollment import SOCKET_SCHEMA, load_application_gate_enrollment
        _require(self._pid == os.getpid() and type(timeout_seconds) in (int, float)
                 and math.isfinite(timeout_seconds) and 0 < timeout_seconds <= 5)
        deadline = time.monotonic() + timeout_seconds
        if not self._mutex.acquire(timeout=max(0, deadline - time.monotonic())):
            raise LaunchSupervisorError()
        try:
            _require(self._journal.start_mode and self._gate_listener is None and self._gate_parent is None)
            self._validate_live(deadline)
            enrolled = load_application_gate_enrollment(enrollment_file, manifest=manifest,
                image=image, supervisor=self)
            _require(enrolled.schema == SOCKET_SCHEMA)
            directory = _path(enrolled.socket_source_directory)
            path = directory / 'controller.sock'
            parent = _root_parent(path, private=False)
            self._gate_parent, self._gate_directory = parent, directory
            info = os.fstat(parent)
            _require(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o711
                     and _identity(os.lstat(directory)) == _identity(info)
                     # The daemon and namespaced app must be able to traverse
                     # every ancestor without granting directory listing.
                     and all(os.stat(ancestor, follow_symlinks=False).st_mode & 0o001
                             for ancestor in directory.parents))
            try:
                os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise LaunchSupervisorError()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
            self._gate_listener = listener
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            endpoint = f'/proc/self/fd/{parent}/{path.name}'
            _require(len(endpoint.encode()) <= 107)
            listener.bind(endpoint)
            first = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            _require(stat.S_ISSOCK(first.st_mode))
            self._gate_socket_identity = _identity(first)
            os.chown(path.name, 0, enrolled.consumer_host_gid, dir_fd=parent, follow_symlinks=False)
            os.chmod(path.name, 0o660, dir_fd=parent, follow_symlinks=False)
            current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            _require(_identity(current) == self._gate_socket_identity and current.st_uid == 0
                     and current.st_gid == enrolled.consumer_host_gid and stat.S_IMODE(current.st_mode) == 0o660)
            listener.listen(1)
            self._gate_enrollment = enrolled
            self._validate_live(deadline)
            _require(load_application_gate_enrollment(enrollment_file, manifest=manifest,
                image=image, supervisor=self) == enrolled)
            self._gate_socket_check(enrolled)
            _remaining(deadline)
            return enrolled.socket_source_directory
        except BaseException:
            self._hold()
            raise LaunchSupervisorError() from None
        finally:
            self._mutex.release()

    def _gate_socket_check(self, enrolled):
        """Recheck the retained parent and socket; caller holds the lifecycle lock."""
        _require(self._gate_enrollment == enrolled and self._gate_parent is not None
                 and self._gate_listener is not None and self._gate_directory is not None)
        directory = os.lstat(self._gate_directory)
        endpoint = os.stat('controller.sock', dir_fd=self._gate_parent, follow_symlinks=False)
        _require(stat.S_ISDIR(directory.st_mode) and directory.st_uid == 0
                 and stat.S_IMODE(directory.st_mode) == 0o711
                 and _identity(directory) == _identity(os.fstat(self._gate_parent))
                 and stat.S_ISSOCK(endpoint.st_mode) and _identity(endpoint) == self._gate_socket_identity
                 and endpoint.st_uid == 0 and endpoint.st_gid == enrolled.consumer_host_gid
                 and stat.S_IMODE(endpoint.st_mode) == 0o660
                 and all(os.stat(ancestor, follow_symlinks=False).st_mode & 0o001
                         for ancestor in self._gate_directory.parents))

    def accept_gate_channel(self, *, manifest, image, enrollment_file, host_pid,
                            process_start_ticks, timeout_seconds):
        """Accept once; authenticate the app in this supervisor's namespaces.

        The independently measured host PID/start must refer to the enrolled
        app container. SO_PEERCRED is interpreted here, not inside the app PID
        namespace, where the outside supervisor may be represented as PID zero.
        The supervisor retains both returned objects until close; no descriptor
        custody or sole-writer property is established by this exchange.
        """
        from .application_gate_enrollment import SOCKET_SCHEMA, load_application_gate_enrollment
        from .broker_seqpacket import PeerPin
        _require(self._pid == os.getpid() and type(host_pid) is int and host_pid > 0
                 and type(process_start_ticks) is int and process_start_ticks > 0
                 and type(timeout_seconds) in (int, float) and math.isfinite(timeout_seconds)
                 and 0 < timeout_seconds <= 5)
        deadline = time.monotonic() + timeout_seconds
        if not self._mutex.acquire(timeout=max(0, deadline - time.monotonic())):
            raise LaunchSupervisorError()
        channel = peer = None
        try:
            _require(not self._gate_accept_used and self._gate_listener is not None and self._gate_parent is not None)
            self._gate_accept_used = True
            self._validate_live(deadline)
            enrolled = load_application_gate_enrollment(enrollment_file, manifest=manifest,
                image=image, supervisor=self)
            _require(enrolled.schema == SOCKET_SCHEMA)
            self._gate_socket_check(enrolled)
            self._gate_listener.settimeout(_remaining(deadline))
            channel, _ = self._gate_listener.accept()
            channel.set_inheritable(False)
            channel.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            credentials = struct.unpack('=iII', channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            _require(credentials == (host_pid, enrolled.consumer_host_uid, enrolled.consumer_host_gid))
            peer = PeerPin(pid=host_pid, uid=enrolled.consumer_host_uid,
                           gid=enrolled.consumer_host_gid, start_ticks=process_start_ticks)
            status = {}
            for line in Path(f'/proc/{host_pid}/status').read_text().splitlines():
                key, separator, value = line.partition(':')
                if separator and key in {'Uid', 'Gid', 'NSpid'}:
                    _require(key not in status)
                    status[key] = value.split()
            _require(status.get('Uid') == [str(enrolled.consumer_host_uid)] * 4
                     and status.get('Gid') == [str(enrolled.consumer_host_gid)] * 4
                     and status.get('NSpid') and status['NSpid'][-1] == '1'
                     and _mapped_id(Path(f'/proc/{host_pid}/uid_map').read_text(), 10001) == enrolled.consumer_host_uid
                     and _mapped_id(Path(f'/proc/{host_pid}/gid_map').read_text(), 10001) == enrolled.consumer_host_gid)
            peer.check()
            self._validate_live(deadline)
            _require(load_application_gate_enrollment(enrollment_file, manifest=manifest,
                image=image, supervisor=self) == enrolled)
            self._gate_socket_check(enrolled)
            _remaining(deadline)
            self._gate_accepted_channel, self._gate_accepted_peer = channel, peer
            return channel, peer
        except BaseException:
            if channel is not None: channel.close()
            if peer is not None: peer.close()
            self._hold()
            raise LaunchSupervisorError() from None
        finally:
            self._mutex.release()

    def serve_one_gate_start_enrollment(self, *, channel, peer, expected_without_nonce,
                                        manifest, image, enrollment_file, controller_key_file,
                                        verify_live, timeout_seconds):
        """Issue once through the v3 journal; trusted app/custody verification remains external.

        The caller retains channel/peer ownership. No installed startup path
        enables this method. A send is not a delivery acknowledgement; on any
        error the supervisor holds, and an attempted reservation is never retried.
        The callback must independently verify application identity, owner,
        fence, mount custody and measured bootstrap under the host trust model.
        It runs under the supervisor mutex and must not call any supervisor
        method that acquires that mutex; recursive entry here is denied. It
        must be bounded and synchronous: a blocked callback cannot be canceled
        by this deadline, and the owner must hold the operation until it exits.
        """
        from .application_gate_enrollment import STATIC_SCHEMA, SOCKET_SCHEMA, load_application_gate_enrollment
        from .mount_gate_start_enrollment import MountGateStartEnrollmentController
        _require(self._pid == os.getpid() and type(timeout_seconds) in (int, float)
                 and math.isfinite(timeout_seconds) and 0 < timeout_seconds <= 5
                 and getattr(self, '_gate_start_thread', None) != threading.get_ident())
        deadline = time.monotonic() + timeout_seconds
        if not self._mutex.acquire(timeout=max(0, deadline - time.monotonic())):
            raise LaunchSupervisorError()
        try:
            self._gate_start_thread = threading.get_ident()
            held = False
            def hold(_pins=None):
                nonlocal held
                if not held:
                    held = True
                    self._hold()
            try:
                _require(self._journal.start_mode)
                _remaining(deadline)
                base = json.loads(_canonical(expected_without_nonce))
                self._validate_live(deadline)
                enrolled = load_application_gate_enrollment(enrollment_file, manifest=manifest,
                    image=image, supervisor=self)
                _require(enrolled.schema in (STATIC_SCHEMA, SOCKET_SCHEMA))
                if enrolled.schema == SOCKET_SCHEMA:
                    _require(enrolled == self._gate_enrollment
                             and channel is self._gate_accepted_channel and peer is self._gate_accepted_peer
                             and type(base) is dict
                             and base.get('manifest_digest') == enrolled.manifest_digest
                             and base.get('image_config_id') == enrolled.image_config_id
                             and base.get('runnable_digest') == enrolled.runnable_digest
                             and base.get('platform_manifest_digest') == enrolled.platform_manifest_digest
                             and base.get('implementation_commit') == enrolled.source_commit)
                    self._gate_socket_check(enrolled)
                key_path = _path(controller_key_file)
                key, key_identity = _root_file(key_path, 32)
                _require(len(key) == 32)
                signer = Ed25519PrivateKey.from_private_bytes(key)
                del key
                record = json.loads(_canonical(self._record()))
                supervisor_id = self._launch_id
                _require(type(supervisor_id) is str)

                def current(pins):
                    self._validate_live(deadline)
                    current_enrollment = load_application_gate_enrollment(enrollment_file,
                        manifest=manifest, image=image, supervisor=self)
                    _require(current_enrollment == enrolled and _root_file(key_path, 32)[1] == key_identity
                             and self._launch_id == supervisor_id and self._record() == record
                             and callable(verify_live) and verify_live(pins) is True)
                    self._validate_live(deadline)
                    _require(load_application_gate_enrollment(enrollment_file, manifest=manifest,
                        image=image, supervisor=self) == enrolled and _root_file(key_path, 32)[1] == key_identity
                        and self._launch_id == supervisor_id and self._record() == record)
                    if enrolled.schema == SOCKET_SCHEMA:
                        self._gate_socket_check(enrolled)
                    _remaining(deadline)
                    return True

                controller = MountGateStartEnrollmentController(channel=channel, peer=peer,
                    expected_without_nonce=base, bootstrap_digest=enrolled.bootstrap_digest,
                    signing_key=signer, enrolled_public_key=enrolled.controller_public_key,
                    verify_live=current,
                    reserve_start=lambda payload, digest: self._journal.reserve_gate_start(
                        supervisor_id, record, base, enrolled.bootstrap_digest, payload, digest),
                    hold_uncertain=hold, timeout_seconds=min(timeout_seconds, _remaining(deadline)))
                return controller.serve_once()
            except BaseException:
                if self._journal is not None:
                    with self._journal._gate_lock:
                        self._journal._gate_fresh = False
                hold()
                raise LaunchSupervisorError() from None
        finally:
            self._gate_start_thread = None
            self._mutex.release()

    def serve_one_coordinator_handoff(self, channel):
        """Issue one measured-coordinator endpoint receipt; pair launch remains disabled."""
        return self._serve_one_role_handoff(channel, 'coordinator')

    def _serve_one_role_handoff(self, channel, role):
        """One measured single-role FD receipt; pair launch remains hard-disabled.

        Transfers ownership of an already connected channel to this method. The
        trusted pair supervisor must have created and retained both ends; this
        single-role step does not attest the other end, its owner, exclusive
        custody, or pair identity and must never be treated as pair authentication
        or launch approval. Constructor launch gates remain unchanged.
        Its journal reservation is the same one-shot issuance as the ordinary
        receipt, so neither version can be issued after the other. A lost/partial
        send is held, never retried; this method always closes its channel copy.
        """
        _require(self._pid == os.getpid())
        with self._mutex:
            peer = None
            reserved = False
            try:
                self._guard()
                _require(type(channel) is socket.socket and channel.family == socket.AF_UNIX
                         and channel.type & socket.SOCK_SEQPACKET == socket.SOCK_SEQPACKET
                         and channel.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 0
                         and (getattr(self.profile, 'role', None) or 'executor') == role
                         and getattr(self.profile, 'command_channel', None) == 'inherited-seqpacket-v1'
                         and self._issuance_open and time.monotonic() < self._expires)
                channel.getpeername()
                info = os.fstat(channel.fileno())
                _require(stat.S_ISSOCK(info.st_mode))
                deadline = min(self._expires, time.monotonic() + self._request_ms / 1000)
                self._listener.settimeout(_remaining(deadline))
                peer, _ = self._listener.accept()
                credentials = struct.unpack('=iII', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                _require(credentials == (self._consumer.pid, self._consumer_uid, self._consumer_gid))
                self._validate_live(deadline)
                size = struct.unpack('!I', self._receive(peer, 4, deadline))[0]
                _require(0 < size <= MAX_REQUEST_BYTES)
                request = _strict(self._receive(peer, size, deadline), MAX_REQUEST_BYTES)
                _require(type(request) is dict and set(request) == {'protocol', 'nonce'}
                         and request['protocol'] == ('engineering-hub.launch-channel-request.v1' if role == 'executor'
                             else 'engineering-hub.coordinator-channel-request.v1')
                         and type(request['nonce']) is str and _HEX.fullmatch(request['nonce']))
                peer.settimeout(_remaining(deadline))
                _require(peer.recv(1) == b'')
                self._validate_live(deadline)
                manifest = self._handoff_manifest_digest()
                self._journal.reserve_issuance(self._launch_id, request['nonce'])
                reserved = True
                self._issuance_open = False
                self._validate_live(deadline)
                now = int(time.time() * 1000)
                remaining = int((self._expires - time.monotonic()) * 1000)
                _require(remaining > 0)
                payload = {'protocol': ('engineering-hub.launch-channel.v1' if role == 'executor'
                                        else 'engineering-hub.coordinator-channel.v1'),
                    'audience': ('engineering-hub.container-executor' if role == 'executor'
                                 else 'engineering-hub.pilot-coordinator'), 'nonce': request['nonce'],
                    'role': role, 'launch_id': self._launch_id, 'boot_id': self._consumer.boot,
                    'authenticated_host_pid': self._consumer.pid, 'process_start_ticks': self._consumer.start,
                    'container_id': self._container_id, 'issuer_id': self.release.issuer_id,
                    'implementation_commit': self.release.implementation_commit,
                    'runnable_digest': self.release.runnable_digest,
                    'platform_manifest_digest': self.release.platform_manifest_digest,
                    'image_config_id': self.release.image_config_id,
                    'platform': self.release.platform.model_dump(mode='json'),
                    'launch_profile_digest': self.release.launch_profile_digest,
                    'manifest_digest': manifest, 'channel_device': info.st_dev, 'channel_inode': info.st_ino,
                    'issued_at_ms': now, 'expires_at_ms': min(now + remaining, self._approval_expires_at_ms)}
                _require(payload['expires_at_ms'] > now)
                domain = (b'engineering-hub-launch-channel-v1\x00' if role == 'executor'
                          else b'engineering-hub-coordinator-channel-v1\x00')
                sig = self._signer.sign(domain + _canonical(payload))
                signature = base64.urlsafe_b64encode(sig).rstrip(b'=').decode('ascii')
                raw = _canonical({'payload': payload, 'signature': signature})
                _require(len(raw) <= MAX_RECEIPT_BYTES)
                self._validate_live(deadline)
                peer.settimeout(_remaining(deadline))
                packet = struct.pack('!I', len(raw)) + raw
                sent = peer.sendmsg([packet], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                               array.array('i', [channel.fileno()]))])
                _require(sent == len(packet))
                _remaining(deadline)
                receipt_hash = hashlib.sha256(raw).hexdigest()
                self._journal.issuance_state(self._launch_id, 'sent', receipt_hash)
                # This durable one-shot state is never reverted after the first
                # descriptor send: final confirmation delivery can be unknown.
                reserved = False
                self._validate_live(deadline)
                commit = {'protocol': ('engineering-hub.launch-channel-commit.v1' if role == 'executor'
                                       else 'engineering-hub.coordinator-channel-commit.v1'),
                          'nonce': request['nonce'], 'launch_id': self._launch_id,
                          'receipt_digest': 'sha256:' + receipt_hash}
                commit_domain = (b'engineering-hub-launch-channel-commit-v1\x00' if role == 'executor'
                                 else b'engineering-hub-coordinator-channel-commit-v1\x00')
                commit_sig = self._signer.sign(commit_domain + _canonical(commit))
                commit_raw = _canonical({'payload': commit, 'signature':
                    base64.urlsafe_b64encode(commit_sig).rstrip(b'=').decode('ascii')})
                _require(len(commit_raw) <= MAX_RECEIPT_BYTES)
                peer.settimeout(_remaining(deadline))
                peer.sendall(struct.pack('!I', len(commit_raw)) + commit_raw)
                peer.shutdown(socket.SHUT_WR)
                self._receipt_served = True
                return payload['launch_id']
            except BaseException:
                if reserved:
                    try:
                        self._journal.issuance_state(self._launch_id, 'held', None)
                    except BaseException:
                        pass
                self._hold()
                raise LaunchSupervisorError() from None
            finally:
                if peer is not None:
                    peer.close()
                if type(channel) is socket.socket:
                    channel.close()

    @contextmanager
    def _observation_mutex(self, deadline, *, wait_window=False):
        # PID fence is required before a possibly inherited mutex.
        _require(self._pid == os.getpid())
        if getattr(self, '_observation_thread', None) == threading.get_ident():
            self._hold()
            raise LaunchSupervisorError()
        def remaining():
            value = deadline - time.monotonic()
            _require(math.isfinite(value))
            if wait_window and value <= 0:
                raise _ObservationWaitExpired()
            _require(value > 0)
            return value
        acquired = self._mutex.acquire(timeout=remaining())
        if wait_window and not acquired:
            raise _ObservationWaitExpired()
        _require(acquired)
        try:
            remaining()
            self._observation_thread = threading.get_ident()
            yield
        finally:
            self._observation_thread = None
            self._mutex.release()

    def _observe_current_launch_locked(self, nonce, deadline):
        """Only identity/authority failures here hold; wire input is checked outside."""
        try:
            self._guard()
            _require(self._receipt_served and not self._issuance_open)
            deadline = min(deadline, self._expires)
            _remaining(deadline)
            self._validate_live(deadline)
            now = int(time.time() * 1000)
            _require(type(self._approval_expires_at_ms) is int
                     and 0 < now < self._approval_expires_at_ms)
            release = current_observation.CurrentExecutorReleasePins(**{
                name: getattr(self.release, name)
                for name in current_observation.CurrentExecutorReleasePins.__dataclass_fields__})
            pins = current_observation.CurrentLaunchPins(
                self._launch_id, self._consumer.boot, self._consumer.pid, self._consumer.start,
                self._container_id, self._daemon_pin.daemon_id, self._daemon_pin.pid,
                self._daemon_pin.start_ticks, self._approval_expires_at_ms)
            # Fixed original wall expiry AND retained local monotonic expiry.
            remaining = int(_remaining(self._expires) * 1000)
            expires = min(now + min(5000, remaining), self._approval_expires_at_ms)
            _require(now < expires)
            payload = {name: getattr(release, name) for name in current_observation._RELEASE_FIELDS}
            payload['platform'] = release.platform.model_dump(mode='json')
            payload.update({name: getattr(pins, name) for name in current_observation._LAUNCH_FIELDS})
            payload.update(protocol=current_observation.PROTOCOL, audience=current_observation.AUDIENCE,
                           nonce=nonce, observation_id=str(uuid4()), observed_at_ms=now, expires_at_ms=expires)
            signature = base64.urlsafe_b64encode(self._signer.sign(
                current_observation.SIGNATURE_DOMAIN + _canonical(payload))).rstrip(b'=').decode()
            raw = _canonical({'payload': payload, 'signature': signature})
            _require(len(raw) <= current_observation.MAX_OBSERVATION_BYTES)
            self._validate_live(deadline)
            _require(int(time.time() * 1000) < expires)
            _remaining(deadline)
            return raw
        except BaseException:
            self._hold()
            raise LaunchSupervisorError() from None

    def observe_current_launch(self, *, nonce, timeout_ms):
        """Fresh signed current identity; no create/start/stop or approval renewal.

        Trusted Python entry point. Socket callers must pass the separate exact
        init-peer authentication below; a nonce is not authority to select a PID.
        """
        _require(self._pid == os.getpid())
        _require(type(nonce) is str and _HEX.fullmatch(nonce)
                 and type(timeout_ms) is int and 1 <= timeout_ms <= 10000)
        deadline = time.monotonic() + min(timeout_ms, self._request_ms) / 1000
        with self._observation_mutex(deadline):
            return self._observe_current_launch_locked(nonce, deadline)

    def serve_one_current_observation(self, *, timeout_ms):
        """One bounded peer; malformed/foreign input never holds a healthy launch.

        False means rejected wire input/peer/transport. Actual identity failure
        raises with held original evidence; neither outcome stops the container.
        """
        _require(self._pid == os.getpid())
        _require(type(timeout_ms) is int and 1 <= timeout_ms <= 10000)
        deadline = time.monotonic() + min(timeout_ms, self._request_ms) / 1000
        return self._serve_current_until(deadline)

    def _serve_current_until(self, deadline):
        # Carry an absolute bound across the owned wait's request handoff.
        # Scheduling or mutex admission cannot mint a fresh relative window.
        try:
            return self._serve_current_peer_until(deadline)
        except _ObservationWaitExpired:
            return False

    def _serve_current_peer_until(self, deadline):
        with self._observation_mutex(deadline, wait_window=True):
            peer = None
            try:
                # No framing or remote request can select the retained consumer.
                self._listener.settimeout(_remaining(deadline))
                peer, _ = self._listener.accept()
                credentials = struct.unpack('=iII', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                _require(credentials == (self._consumer.pid, self._consumer_uid, self._consumer_gid))
                size = struct.unpack('!I', self._receive(peer, 4, deadline))[0]
                _require(0 < size <= MAX_REQUEST_BYTES)
                request = _strict(self._receive(peer, size, deadline), MAX_REQUEST_BYTES)
                _require(type(request) is dict and set(request) == {'protocol', 'nonce'}
                         and request['protocol'] == CURRENT_REQUEST_PROTOCOL
                         and type(request['nonce']) is str and _HEX.fullmatch(request['nonce']))
                peer.settimeout(_remaining(deadline))
                _require(peer.recv(1) == b'')
            except Exception:
                if peer is not None:
                    peer.close()
                return False
            except BaseException:
                if peer is not None:
                    peer.close()
                raise
            try:
                raw = self._observe_current_launch_locked(request['nonce'], deadline)
                # A disconnected requester is not proof of a broken launch.
                try:
                    peer.settimeout(_remaining(deadline))
                    peer.sendall(struct.pack('!I', len(raw)) + raw)
                    peer.shutdown(socket.SHUT_WR)
                    _remaining(deadline)
                    return True
                except Exception:
                    return False
            finally:
                peer.close()

    def wait_for_stop_with_observations(self, *, timeout_ms, stop_requested):
        """Owned bounded wait, serving fresh observations after the one receipt.

        No new listener/thread/service. Approval expiry stops observation service,
        not the container; only the caller's explicit stop signal requests stop.
        """
        _require(self._pid == os.getpid())
        _require(type(timeout_ms) is int and 1 <= timeout_ms <= 3_600_000
                 and callable(stop_requested))
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            requested = stop_requested()
            _require(type(requested) is bool)
            if requested:
                return True
            if time.monotonic() >= deadline:
                return False
            try:
                with self._observation_mutex(deadline, wait_window=True):
                    try:
                        _require(not self._closed and not self._held and self._receipt_served
                                 and not self._issuance_open and self._consumer is not None)
                        self._consumer.check()
                        active = (time.monotonic() < self._expires
                                  and int(time.time() * 1000) < self._approval_expires_at_ms)
                        read = [self._consumer.fd]
                        if active:
                            read.append(self._listener)
                        remaining = deadline - time.monotonic()
                        _require(math.isfinite(remaining))
                        if remaining <= 0:
                            return False
                        ready, _, _ = select.select(read, [], [], min(.1, remaining))
                        _require(self._consumer.fd not in ready)
                        pending = active and self._listener in ready
                    except BaseException:
                        self._hold()
                        raise LaunchSupervisorError() from None
            except _ObservationWaitExpired:
                return False
            if pending:
                self._serve_current_until(min(
                    deadline, time.monotonic() + self._request_ms / 1000))

    def graceful_stop(self):
        """Explicit SIGTERM only. Timeout is held, never permission for SIGKILL."""
        _require(self._pid == os.getpid())
        with self._mutex:
            try:
                self._guard(allow_held=True)
                self._issuance_open = False
                deadline = time.monotonic() + self._launch_ms / 1000
                self._validate_live(deadline, allow_held=True)
                self._journal.state(self._launch_id, 'term_reserved', self._record())
                self._engine.json('POST', '/containers/' + self._container_id + '/kill?signal=SIGTERM',
                                  deadline, statuses=(204,))
                _require(bool(select.select([self._consumer.fd], [], [], _remaining(deadline))[0]))
                self._wait_stopped(deadline)
                self._journal.state(self._launch_id, 'stopped', self._record())
                self._remove_owned_socket()
            except BaseException:
                self._hold()
                raise LaunchSupervisorError() from None

    def _remove_owned_socket(self):
        _require(self._socket_parent is not None and self._socket_identity is not None
                 and _identity(os.stat(self._socket_path.parent, follow_symlinks=False))
                     == _identity(os.fstat(self._socket_parent)))
        info = os.stat(self._socket_path.name, dir_fd=self._socket_parent, follow_symlinks=False)
        _require(_identity(info) == self._socket_identity and stat.S_ISSOCK(info.st_mode)
                 and info.st_uid == 0 and info.st_gid == self._consumer_gid
                 and stat.S_IMODE(info.st_mode) == 0o660)
        os.unlink(self._socket_path.name, dir_fd=self._socket_parent)
        os.fsync(self._socket_parent)
        self._socket_identity = None

    def close(self):
        """Close owned FDs only; unknown/live containers and socket evidence remain."""
        # Never wait on a mutex inherited across fork. The child can close only
        # its own descriptor copies; no flock unlock, journal update or unlink.
        if self._pid != os.getpid():
            self._close_descriptors()
            return
        with self._mutex:
            self._close_descriptors()

    def _close_descriptors(self):
        self._issuance_open = False
        self._closed = True
        failed = False
        for name in ('_listener', '_socket_parent', '_gate_listener', '_gate_parent', '_gate_accepted_channel',
                     '_gate_accepted_peer', '_consumer', '_engine', '_journal'):
            value = getattr(self, name, None)
            if value is not None:
                try:
                    if name in ('_socket_parent', '_gate_parent'):
                        os.close(value)
                    else:
                        value.close()
                except BaseException:
                    failed = True
                finally:
                    setattr(self, name, None)
        self._signer = None
        if failed:
            raise LaunchSupervisorError() from None
