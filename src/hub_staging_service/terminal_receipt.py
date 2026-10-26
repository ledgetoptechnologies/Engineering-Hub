"""Bounded secondary evidence journal, never execution or cleanup authority.

Every slot is fully written and fsynced before admission. Existing-block writes
avoid new terminal files, append growth and rename allocation. This is NOT an
ENOSPC/fsync guarantee: ext4 journal metadata, CoW/shared extents, device errors
and other writers still require operator filesystem acceptance. The private
receipt root must never be mounted into source containers. Slots are not reused
or removed here. SQLite remains authoritative; recovery only returns evidence.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .core import Rejected, ensure


FRAME_BYTES = 64 * 1024
SLOT_BYTES = 2 * FRAME_BYTES
MAX_JSON_BYTES = 48 * 1024
_BLANK = b'\xa5'
_MAGIC = b'EH-PRIVATE-RECEIPT-V1\n'
_COMMIT = b'EH-FRAME-COMMIT!\n'
_FOOTER_BYTES = 32 + len(_COMMIT)


def _id(value):
    try:
        ensure(type(value) is str and str(uuid.UUID(value)) == value, 'receipt_identity_invalid')
    except (ValueError, TypeError, AttributeError):
        raise Rejected('receipt_identity_invalid') from None
    return value


def _sha(value):
    ensure(type(value) is str and re.fullmatch('[0-9a-f]{64}', value), 'receipt_identity_invalid')
    return value


def _pair(value):
    ensure(type(value) in (list, tuple) and len(value) == 2 and
           all(type(n) is int and n >= 0 for n in value), 'receipt_identity_invalid')
    return list(value)


def _binding(value):
    common = {'command_id', 'job_id', 'operation_identity', 'worker_id', 'journal_identity', 'lease_identity'}
    ensure(type(value) is dict, 'receipt_binding_invalid')
    cleanup = value.get('operation_kind') == 'cleanup'
    ensure(set(value) == (common | {'operation_kind', 'resource_root_identity', 'resources'} if cleanup else common),
           'receipt_binding_invalid')
    for key in ('command_id', 'worker_id'):
        _id(value[key])
    _sha(value['operation_identity']); _pair(value['journal_identity'])
    if cleanup:
        ensure(value['job_id'] is None and value['lease_identity'] is None
               and type(value['resources']) is list and 1 <= len(value['resources']) <= 4,
               'receipt_binding_invalid')
        _pair(value['resource_root_identity'])
        identities = []
        for item in value['resources']:
            ensure(type(item) is dict and set(item) == {'resource_id', 'job_id', 'kind', 'revision', 'inode', 'manifest_sha256'},
                   'receipt_binding_invalid')
            identities.append(_id(item['resource_id'])); _id(item['job_id']); _sha(item['manifest_sha256'])
            ensure(item['kind'] in ('workspace', 'build', 'test_run', 'preview')
                   and type(item['revision']) is int and item['revision'] >= 1
                   and type(item['inode']) is int and item['inode'] > 0, 'receipt_binding_invalid')
        ensure(len(set(identities)) == len(identities), 'receipt_binding_invalid')
    else:
        _id(value['job_id'])
        if value['lease_identity'] is not None:
            _sha(value['lease_identity'])
    return json.loads(_json(value))


def _binding_schema(original):
    return ('engineering-hub.terminal-receipt-binding.v2' if original.get('operation_kind') == 'cleanup'
            else 'engineering-hub.terminal-receipt-binding.v1')


def _runtime(value):
    if value is None:
        return
    ensure(type(value) is dict and value and set(value) <= {'engine_id', 'boot_id', 'invocation_id'},
           'receipt_runtime_invalid')
    for key, item in value.items():
        if key == 'engine_id':
            ensure(type(item) is str and re.fullmatch('[A-Za-z0-9:_-]{1,128}', item), 'receipt_runtime_invalid')
        else:
            _id(item)


def _json(value):
    # Inputs are trusted bounded metadata, never logs, source or credentials.
    # Count the exact ensure_ascii/separators JSON byte budget BEFORE dumps.
    # Repeated references to large Unicode strings must not allocate a giant
    # serialized document just to discover the final document exceeds its cap.
    count = [0]
    budget = [0]
    def add(size):
        budget[0] += size
        ensure(budget[0] <= MAX_JSON_BYTES, 'receipt_metadata_too_large')
    def string(item):
        ensure(len(item) <= MAX_JSON_BYTES, 'receipt_metadata_too_large')
        add(2)
        for char in item:
            code = ord(char)
            if char in ('"', '\\', '\b', '\f', '\n', '\r', '\t'):
                add(2)
            elif code < 32 or 127 <= code <= 65535:
                add(6)
            elif code > 65535:
                add(12)
            else:
                add(1)
    def check(item, depth=0):
        count[0] += 1
        ensure(depth <= 16 and count[0] <= 2048, 'receipt_metadata_too_large')
        if type(item) is str:
            string(item)
        elif type(item) is dict:
            ensure(len(item) <= 256, 'receipt_metadata_too_large')
            add(2 + max(0, len(item)-1))
            for key, child in item.items():
                ensure(type(key) is str and len(key) <= 128, 'receipt_metadata_invalid')
                string(key); add(1)
                check(child, depth + 1)
        elif type(item) in (list, tuple):
            ensure(len(item) <= 256, 'receipt_metadata_too_large')
            add(2 + max(0, len(item)-1))
            for child in item:
                check(child, depth + 1)
        else:
            ensure(item is None or type(item) in (int, bool), 'receipt_metadata_invalid')
            if type(item) is int:
                ensure(-2**63 <= item < 2**63, 'receipt_metadata_invalid')
                add(len(str(item)))
            else:
                add(4 if item is None or item is True else 5)
    check(value)
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    ensure(len(raw) == budget[0] and len(raw) <= MAX_JSON_BYTES, 'receipt_metadata_too_large')
    return raw


def _frame(value):
    raw = _json(value)
    body = _MAGIC + f'{len(raw):08x}\n'.encode('ascii') + raw
    body += _BLANK * (FRAME_BYTES - _FOOTER_BYTES - len(body))
    return body + hashlib.sha256(body).digest() + _COMMIT


def _decode(raw):
    ensure(type(raw) is bytes and len(raw) == FRAME_BYTES, 'receipt_frame_invalid')
    if raw == _BLANK * FRAME_BYTES:
        return None
    body, footer = raw[:-_FOOTER_BYTES], raw[-_FOOTER_BYTES:]
    ensure(body.startswith(_MAGIC) and footer[32:] == _COMMIT and
           footer[:32] == hashlib.sha256(body).digest(), 'receipt_frame_invalid')
    length = body[len(_MAGIC):len(_MAGIC)+9]
    ensure(re.fullmatch(rb'[0-9a-f]{8}\n', length), 'receipt_frame_invalid')
    size = int(length[:-1], 16)
    ensure(0 < size <= MAX_JSON_BYTES, 'receipt_frame_invalid')
    start = len(_MAGIC) + 9
    ensure(body[start+size:] == _BLANK * (len(body)-start-size), 'receipt_frame_invalid')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            ensure(key not in result, 'receipt_frame_invalid')
            result[key] = value
        return result
    try:
        value = json.loads(body[start:start+size], object_pairs_hook=unique)
        ensure(_json(value) == body[start:start+size], 'receipt_frame_invalid')
        return value
    except (ValueError, TypeError, RecursionError):
        raise Rejected('receipt_frame_invalid') from None


class TerminalReceipt:
    """One immutable original binding and one terminal observation per slot.

    Caller owns serialization and holds its worker lifetime lock. `capture()`
    must be persisted with admission facts when possible. Reopening with that
    capture detects replacement. Without it, read evidence cannot establish
    original inode identity; neither mode grants any recovery authority.
    """
    @classmethod
    def prepare(cls, private_root, slot_id):
        root = cls._root(private_root)
        name = 'receipt-' + _id(slot_id) + '.bin'
        root_fd = cls._open_root(root)
        fd = None
        try:
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=root_fd)
            # Actual writes, not sparse truncate/fallocate-only reservation.
            cls._write_all(fd, _BLANK * SLOT_BYTES, 0)
            os.fsync(fd); os.fsync(root_fd)
            ensure(os.fstat(fd).st_blocks * 512 >= SLOT_BYTES, 'receipt_capacity_unverified')
        finally:
            if fd is not None:
                os.close(fd)
            os.close(root_fd)
        # Failed preparation deliberately leaves an unaccepted, unusable slot.
        return cls(root, slot_id)

    @staticmethod
    def _root(value):
        ensure(os.name == 'posix' and hasattr(os, 'pwrite'), 'receipt_posix_required')
        root = Path(value)
        ensure(root.is_absolute() and all(p not in ('.', '..') for p in root.parts), 'receipt_private_root_required')
        return root

    @staticmethod
    def _open_root(root):
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in root.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = child
            info = os.fstat(fd)
            ensure(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
                   'receipt_private_root_required')
            return fd
        except BaseException:
            os.close(fd)
            raise

    def __init__(self, private_root, slot_id, *, expected_capture=None):
        self.root = self._root(private_root)
        self._io_failed = False
        self.slot_id = _id(slot_id)
        self.name = 'receipt-' + self.slot_id + '.bin'
        root_fd = self._open_root(self.root)
        try:
            self._root_identity = _pair([os.fstat(root_fd).st_dev, os.fstat(root_fd).st_ino])
            fd = os.open(self.name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                info = self._file(fd)
                self._file_identity = [info.st_dev, info.st_ino]
            finally:
                os.close(fd)
        finally:
            os.close(root_fd)
        if expected_capture is not None:
            ensure(expected_capture == self.capture(), 'receipt_replaced')

    @staticmethod
    def _file(fd):
        info = os.fstat(fd)
        ensure(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
               and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size == SLOT_BYTES
               and info.st_blocks * 512 >= SLOT_BYTES, 'receipt_file_invalid')
        return info

    def capture(self):
        return {'schema': 'engineering-hub.terminal-receipt-slot.v1', 'slot_id': self.slot_id,
                'root_identity': list(self._root_identity), 'file_identity': list(self._file_identity),
                'bytes': SLOT_BYTES}

    def _open(self):
        root_fd = self._open_root(self.root)
        try:
            info = os.fstat(root_fd)
            ensure([info.st_dev, info.st_ino] == self._root_identity, 'receipt_replaced')
            fd = os.open(self.name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                info = self._file(fd)
                ensure([info.st_dev, info.st_ino] == self._file_identity, 'receipt_replaced')
                return fd
            except BaseException:
                os.close(fd); raise
        finally:
            os.close(root_fd)

    @staticmethod
    def _write_all(fd, raw, offset):
        done = 0
        while done < len(raw):
            count = os.pwrite(fd, raw[done:], offset + done)
            ensure(type(count) is int and 0 < count <= len(raw)-done, 'receipt_short_write')
            done += count

    def _write_frame(self, fd, value, offset):
        raw = _frame(value)
        try:
            self._write_all(fd, raw[:-_FOOTER_BYTES], offset)
            os.fsync(fd)
            self._write_all(fd, raw[-_FOOTER_BYTES:], offset + FRAME_BYTES - _FOOTER_BYTES)
            os.fsync(fd)
            ensure(os.pread(fd, FRAME_BYTES, offset) == raw, 'receipt_readback_failed')
            self._file(fd)
            current = self._open()
            os.close(current)
        except BaseException:
            # A valid checksum after an fsync error is not a durable success.
            # This latch is intentionally not clearable on the live object.
            self._io_failed = True
            raise

    @staticmethod
    def _read_frames(fd):
        raw = os.pread(fd, SLOT_BYTES + 1, 0)
        ensure(len(raw) == SLOT_BYTES, 'receipt_frame_invalid')
        return _decode(raw[:FRAME_BYTES]), _decode(raw[FRAME_BYTES:])

    def bind(self, original):
        ensure(not self._io_failed, 'receipt_persistence_uncertain')
        original = _binding(original)
        value = {'schema': _binding_schema(original), 'sequence': 1,
                 'slot': self.capture(), 'original': original}
        fd = self._open()
        try:
            first, second = self._read_frames(fd)
            ensure(second is None or first == value, 'receipt_binding_conflict')
            if first is not None:
                ensure(first == value, 'receipt_binding_conflict')
                return
            self._write_frame(fd, value, 0)
        finally:
            os.close(fd)

    def record_terminal(self, original, *, outcome, result, runtime=None):
        ensure(not self._io_failed, 'receipt_persistence_uncertain')
        original = _binding(original)
        ensure(outcome in ('succeeded', 'failed', 'held') and type(result) is dict, 'receipt_terminal_invalid')
        _runtime(runtime)
        fd = self._open()
        try:
            first, second = self._read_frames(fd)
            self._validate_binding(first, original)
            value = {'schema': 'engineering-hub.terminal-receipt-observation.v1', 'sequence': 2,
                     'binding_sha256': hashlib.sha256(_json(first)).hexdigest(),
                     'outcome': outcome, 'result': result, 'runtime': runtime}
            if second is not None:
                ensure(second == value, 'receipt_terminal_conflict')
                return
            self._write_frame(fd, value, FRAME_BYTES)
        finally:
            os.close(fd)

    def _validate_binding(self, value, original):
        ensure(type(value) is dict and type(value.get('sequence')) is int and
               value == {'schema': _binding_schema(original), 'sequence': 1,
               'slot': self.capture(), 'original': original}, 'receipt_binding_conflict')

    def evidence(self, original):
        """Return evidence only. Caller must retain holds and independent gates."""
        original = _binding(original)
        if self._io_failed:
            return {'state': 'uncertain', 'reason': 'receipt_persistence_failed'}
        fd = self._open()
        try:
            first, second = self._read_frames(fd)
            self._validate_binding(first, original)
            if second is None:
                return {'state': 'uncertain', 'reason': 'terminal_observation_missing'}
            ensure(type(second) is dict and set(second) == {'schema', 'sequence', 'binding_sha256',
                   'outcome', 'result', 'runtime'} and second['schema'] ==
                   'engineering-hub.terminal-receipt-observation.v1' and type(second['sequence']) is int
                   and second['sequence'] == 2 and second['binding_sha256'] == hashlib.sha256(_json(first)).hexdigest()
                   and second['outcome'] in ('succeeded', 'failed', 'held') and type(second['result']) is dict,
                   'receipt_terminal_invalid')
            _runtime(second['runtime'])
            current = self._open()
            os.close(current)
            return {'state': 'observed', 'authority': 'evidence_only', 'terminal': second}
        finally:
            os.close(fd)
