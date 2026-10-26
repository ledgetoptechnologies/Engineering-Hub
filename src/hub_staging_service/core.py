"""Durable one-operation worker. This journal is not a scheduling queue."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import threading
import time
import uuid
import zipfile

MAX_ARCHIVE = 8 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
KINDS = ('workspace', 'build', 'test_run', 'preview')
TERMINAL = ('succeeded', 'failed')


class Rejected(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def ensure(condition, code, status=400):
    if not condition:
        raise Rejected(code, status)


def storage_limits(capacity_bytes=1024**3, free_floor_bytes=512 * 1024**2):
    """Explicit controller-state budget and underlying filesystem free floor."""
    ensure(type(capacity_bytes) is int and 64 * 1024**2 <= capacity_bytes <= 2**63 - 1,
           'invalid_worker_capacity')
    ensure(type(free_floor_bytes) is int and 512 * 1024**2 <= free_floor_bytes <= 2**63 - 1,
           'invalid_worker_free_floor')
    return capacity_bytes, free_floor_bytes


def identifier(value):
    try:
        ensure(isinstance(value, str) and str(uuid.UUID(value)) == value, 'invalid_uuid')
    except (ValueError, TypeError, AttributeError):
        raise Rejected('invalid_uuid') from None
    return value


def relative(value):
    ensure(isinstance(value, str) and value and '\\' not in value and '\x00' not in value,
           'unsafe_path')
    path = PurePosixPath(value)
    ensure(not path.is_absolute() and all(x not in ('', '.', '..') for x in value.split('/'))
           and ':' not in value, 'unsafe_path')
    return path


def archive(payload):
    ensure(isinstance(payload, str) and len(payload) <= ((MAX_ARCHIVE + 2) // 3) * 4,
           'archive_too_large')
    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, TypeError):
        raise Rejected('invalid_archive_encoding') from None
    ensure(len(raw) <= MAX_ARCHIVE, 'archive_too_large')
    return raw


def extract(raw, destination=None):
    """Validate the whole central directory before creating any archive entries."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
            entries = bundle.infolist()
            ensure(len(entries) <= 5000, 'archive_file_count')
            names, total = set(), 0
            for item in entries:
                relative(item.orig_filename.rstrip('/'))
                name = item.filename.rstrip('/')
                relative(name)
                ensure(name.casefold() not in names, 'duplicate_archive_path')
                names.add(name.casefold())
                mode = item.external_attr >> 16
                kind = stat.S_IFMT(mode)
                ensure(kind in (0, stat.S_IFREG, stat.S_IFDIR) and not item.flag_bits & 1,
                       'unsupported_archive_entry')
                ensure(item.file_size >= 0 and item.compress_size >= 0, 'archive_size')
                total += item.file_size
                ensure(total <= MAX_EXPANDED, 'archive_expansion_limit')
            for item in entries:
                target = destination.joinpath(*relative(item.filename.rstrip('/')).parts) if destination else None
                if item.is_dir():
                    if target:
                        target.mkdir(parents=True, exist_ok=True)
                    continue
                if target:
                    target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(item) as source:
                    written = 0
                    output = target.open('xb') if target else None
                    try:
                        while chunk := source.read(65536):
                            written += len(chunk)
                            ensure(written <= item.file_size, 'archive_size_drift')
                            if output:
                                output.write(chunk)
                    finally:
                        if output:
                            output.close()
                    ensure(written == item.file_size, 'archive_size_drift')
    except (zipfile.BadZipFile, RuntimeError, EOFError, OSError):
        raise Rejected('invalid_archive') from None


def recipe(value):
    ensure(isinstance(value, dict), 'invalid_recipe')
    value = dict(value)
    ensure(re.fullmatch(r'sha256:[0-9a-f]{64}', value.get('image_id', '')) is not None,
           'immutable_image_required')
    for key in ('build_argv', 'test_argv'):
        argv = value.get(key)
        ensure(isinstance(argv, list) and 0 < len(argv) <= 32 and
               all(isinstance(x, str) and 0 < len(x) <= 2048 and '\x00' not in x for x in argv),
               'fixed_argv_required')
    limits = {'timeout_seconds': (1, 900), 'memory_mb': (32, 1024), 'pids': (8, 128),
              'output_bytes': (1024, 64 * 1024 * 1024), 'log_bytes': (1024, 1024 * 1024)}
    for key, (low, high) in limits.items():
        ensure(type(value.get(key)) is int and low <= value[key] <= high, 'invalid_recipe_limit')
    ensure(type(value.get('cpus')) in (int, float) and 0 < value['cpus'] <= 1, 'invalid_recipe_cpu')
    ensure(value['log_bytes'] <= value['output_bytes'] // 4, 'logs_exceed_output_budget')
    value.setdefault('workspace_bytes', min(256 * 1024 * 1024, value['memory_mb'] * 1024 * 1024 // 2))
    ensure(type(value['workspace_bytes']) is int and 65536 <= value['workspace_bytes'] <= value['memory_mb'] * 1024 * 1024 // 2,
           'invalid_workspace_limit')
    return json.loads(json.dumps(value))


def tree_manifest(path, limit):
    result, total = {}, 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        ensure(len(result) + len(dirs) + len(files) <= 5000, 'output_file_count')
        for name in dirs + files:
            item = Path(directory) / name
            ensure(not item.is_symlink(), 'output_symlink')
            ensure(stat.S_ISDIR(item.lstat().st_mode) or stat.S_ISREG(item.lstat().st_mode),
                   'output_special_file')
        for name in files:
            item = Path(directory) / name
            size = item.stat().st_size
            total += size
            ensure(total <= limit, 'output_size_limit')
            digest = hashlib.sha256()
            with item.open('rb') as stream:
                while chunk := stream.read(65536):
                    digest.update(chunk)
            result[item.relative_to(path).as_posix()] = {'bytes': size, 'sha256': digest.hexdigest()}
    return result, total


def make_job_readable(path, limit):
    """Validate an owned immutable tree before exposing it via a read-only bind.

    Rootless Docker maps the host owner to container root; the configured job
    UID therefore needs read/traverse bits. Only this resource tree changes;
    its private worker/resource ancestors remain inaccessible to other hosts.
    """
    tree_manifest(path, limit)
    for directory, dirs, files in os.walk(path, followlinks=False):
        Path(directory).chmod(0o755)
        for name in files:
            (Path(directory) / name).chmod(0o644)


def terminal_receipt_config(value, worker_root):
    """Static optional operator config; no source-controlled receipt paths."""
    if value is None:
        return None
    ensure(type(value) is dict and set(value) == {'path', 'max_slots'}, 'terminal_receipt_config')
    ensure(type(value['path']) is str and type(value['max_slots']) is int
           and len(value['path']) <= 4096 and len(value['path'].encode('utf-8')) <= 4096
           and 1 <= value['max_slots'] <= 256, 'terminal_receipt_config')
    path, root = Path(value['path']), Path(worker_root).absolute()
    ensure(path.is_absolute() and '..' not in path.parts and path.parent == root
           and path.name not in ('resources', 'sources') and ',' not in str(path)
           and '\x00' not in str(path), 'terminal_receipt_private_path')
    return {'path': path, 'max_slots': value['max_slots']}


class Service:
    def __init__(self, root, projects, backend, *, capacity_bytes=1024 * 1024 * 1024,
                 free_floor_bytes=512 * 1024 * 1024, scratch_slot=None, terminal_receipts=None):
        from .scratch import ScratchSlot
        ensure(scratch_slot is None or type(scratch_slot) is ScratchSlot, 'scratch_slot_type')
        self.scratch_slot = scratch_slot
        receipt_config = terminal_receipt_config(terminal_receipts, root)
        self._receipt_config = receipt_config
        self._receipt_fault = None
        self._receipt_io_failed = set()
        self._receipt_io_failed_overflow = False
        capacity_bytes, free_floor_bytes = storage_limits(capacity_bytes, free_floor_bytes)
        self.root = Path(root).absolute()
        ensure(not self.root.is_symlink(), 'worker_root_symlink')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        ensure(not self.root.is_symlink(), 'worker_root_symlink')
        if os.name == 'posix':
            ensure(self.root.stat().st_uid == os.getuid() and not stat.S_IMODE(self.root.stat().st_mode) & 0o077,
                   'private_worker_root_required')
        ensure(not (self.root / 'worker.lock').is_symlink(), 'worker_lock_symlink')
        self.process_lock = (self.root / 'worker.lock').open('a+b')
        if os.name == 'posix':
            import fcntl
            try:
                fcntl.flock(self.process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.process_lock.close()
                raise Rejected('worker_already_running', 409) from None
        self.resources = self.root / 'resources'
        self.resources.mkdir(exist_ok=True, mode=0o700)
        ensure(not self.resources.is_symlink(), 'resource_root_symlink')
        self.sources = self.root / 'sources'
        self.sources.mkdir(exist_ok=True, mode=0o700)
        ensure(not self.sources.is_symlink(), 'source_root_symlink')
        self.projects = {name: {key: recipe(profile) for key, profile in
                         value.get('recipes', {'default': value}).items()} for name, value in projects.items()}
        ensure(bool(self.projects), 'projects_required')
        self.backend, self.capacity_bytes = backend, capacity_bytes
        self.free_floor_bytes = free_floor_bytes
        self.lock, self.active, self.thread = threading.RLock(), None, None
        for name in ('journal.sqlite3', 'journal.sqlite3-wal', 'journal.sqlite3-shm'):
            ensure(not (self.root / name).is_symlink(), 'journal_symlink')
        self.db = sqlite3.connect(self.root / 'journal.sqlite3', check_same_thread=False)
        (self.root / 'journal.sqlite3').chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS operations (
                command_id TEXT PRIMARY KEY, identity TEXT NOT NULL, job_id TEXT UNIQUE,
                state TEXT NOT NULL, payload TEXT NOT NULL, result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS resources (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL, kind TEXT NOT NULL,
                revision INTEGER NOT NULL, state TEXT NOT NULL, inode INTEGER NOT NULL,
                manifest TEXT NOT NULL, bytes INTEGER NOT NULL, refs TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY, project TEXT NOT NULL, digest TEXT NOT NULL,
                bytes INTEGER NOT NULL, inode INTEGER NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS terminal_receipts (
                command_id TEXT PRIMARY KEY, slot_id TEXT NOT NULL UNIQUE,
                root TEXT NOT NULL, capture TEXT NOT NULL, binding TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS terminal_receipt_settings (
                id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS scratch_leases (
                command_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL, capture TEXT NOT NULL, absence TEXT NOT NULL,
                receipt TEXT NOT NULL);
        ''')
        self.db.execute("UPDATE operations SET state='held' WHERE state IN ('running','accepted')")
        # Even a missing/disabled config cannot bypass an existing reservation.
        self.db.execute("UPDATE operations SET state='held' WHERE command_id IN "
                        "(SELECT command_id FROM scratch_leases WHERE state!='released')")
        self.db.commit()

        self._scratch_lock = None
        if scratch_slot is not None:
            try:
                self._scratch_lock = scratch_slot.acquire(self.root, self.db)
            except Exception:
                self.db.close()
                self.process_lock.close()
                raise

        try:
            if receipt_config is not None:
                ensure(os.name == 'posix', 'terminal_receipts_posix_required')
                private = receipt_config['path']
                ensure(not private.is_symlink(), 'terminal_receipt_private_path')
                private.mkdir(mode=0o700, exist_ok=True)
                info = private.stat(follow_symlinks=False)
                ensure(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                       and stat.S_IMODE(info.st_mode) == 0o700
                       and info.st_dev == self.root.stat().st_dev, 'terminal_receipt_private_path')
                recorded = self.db.execute('SELECT config FROM terminal_receipt_settings WHERE id=1').fetchone()
                wanted = json.dumps({'path': str(private), 'max_slots': receipt_config['max_slots']}, sort_keys=True)
                if recorded is None:
                    self.db.execute('INSERT INTO terminal_receipt_settings VALUES (1,?)', (wanted,))
                    self.db.commit()  # Durable opt-in survives a later admission rollback.
                else:
                    ensure(recorded['config'] == wanted, 'terminal_receipt_configuration_changed')
            self._receipt_inventory()
        except Exception:
            self.db.close(); self.process_lock.close()
            if self._scratch_lock is not None:
                os.close(self._scratch_lock)
            raise

    @staticmethod
    def _receipt_json(raw):
        ensure(type(raw) is str and len(raw) <= 128 * 1024, 'terminal_receipt_journal_invalid')
        ensure(len(raw.encode('utf-8')) <= 128 * 1024, 'terminal_receipt_journal_invalid')
        def unique(pairs):
            value = {}
            for key, item in pairs:
                ensure(key not in value, 'terminal_receipt_journal_invalid')
                value[key] = item
            return value
        return json.loads(raw, object_pairs_hook=unique)

    def _receipt_original(self, payload, identity):
        journal = (self.root / 'journal.sqlite3').stat(follow_symlinks=False)
        root = self.root.stat(follow_symlinks=False)
        worker = str(uuid.uuid5(uuid.NAMESPACE_URL, 'engineering-hub-worker:' +
            json.dumps([[root.st_dev, root.st_ino], [journal.st_dev, journal.st_ino]])))
        lease = self.db.execute('SELECT capture FROM scratch_leases WHERE command_id=?',
                                (payload['command_id'],)).fetchone()
        return {'command_id': payload['command_id'], 'job_id': payload['job_id'],
                'operation_identity': identity, 'worker_id': worker,
                'journal_identity': [journal.st_dev, journal.st_ino],
                'lease_identity': hashlib.sha256(lease['capture'].encode()).hexdigest() if lease else None}

    def _receipt_inventory(self):
        """Only validate retained evidence. Never import, release or rerun it."""
        from .terminal_receipt import TerminalReceipt
        try:
            count = self.db.execute('SELECT COUNT(*) FROM terminal_receipts').fetchone()[0]
            if self._receipt_config is None:
                ensure(not count and not self.db.execute('SELECT 1 FROM terminal_receipt_settings').fetchone(),
                       'terminal_receipts_configuration_required')
                return
            root, maximum = self._receipt_config['path'], self._receipt_config['max_slots']
            ensure(count <= maximum, 'terminal_receipt_slot_limit')
            # Bound SQLite metadata before fetching it into Python, including
            # UTF-8 expansion. Never truncate or ignore unknown history.
            bounds = self.db.execute('''SELECT
                MAX(length(CAST(capture AS BLOB))), MAX(length(CAST(binding AS BLOB))),
                MAX(length(CAST(root AS BLOB))), MAX(length(command_id)), MAX(length(slot_id)),
                SUM(length(CAST(capture AS BLOB))+length(CAST(binding AS BLOB))+length(CAST(root AS BLOB)))
                FROM terminal_receipts''').fetchone()
            if count:
                ensure(all(type(value) is int for value in bounds) and
                       bounds[0] <= 128*1024 and bounds[1] <= 128*1024 and bounds[2] <= 4096
                       and bounds[3] <= 36 and bounds[4] <= 36 and bounds[5] <= 8*1024*1024,
                       'terminal_receipt_journal_invalid')
            rows = self.db.execute('SELECT * FROM terminal_receipts LIMIT 256').fetchall()
            entries = []
            with os.scandir(root) as found:
                for item in found:
                    ensure(len(entries) < maximum, 'terminal_receipt_slot_limit')
                    entries.append(item.name)
            ensure(set(entries) == {'receipt-' + row['slot_id'] + '.bin' for row in rows},
                   'terminal_receipt_orphan_or_missing')
            for row in rows:
                ensure(row['root'] == str(root), 'terminal_receipt_root_changed')
                binding = self._receipt_json(row['binding'])
                operation = self.db.execute('SELECT * FROM operations WHERE command_id=?',
                                            (row['command_id'],)).fetchone()
                ensure(operation is not None and operation['job_id'] is not None and
                       binding['command_id'] == row['command_id'] and binding['job_id'] == operation['job_id']
                       and binding['operation_identity'] == operation['identity'], 'terminal_receipt_binding_changed')
                # The original lease intent predates directory materialization;
                # retain its digest, do not recompute it from the later capture.
                expected = self._receipt_original({'command_id': row['command_id'],
                    'job_id': operation['job_id']}, operation['identity'])
                expected['lease_identity'] = binding['lease_identity']
                ensure(binding == expected, 'terminal_receipt_binding_changed')
                receipt = TerminalReceipt(root, row['slot_id'], expected_capture=self._receipt_json(row['capture']))
                evidence = receipt.evidence(binding)
                ensure(evidence['state'] == 'observed' and operation['state'] in TERMINAL,
                       'terminal_receipt_recovery_required')
                observed = evidence['terminal']
                ensure(observed['outcome'] == operation['state'] and
                       json.dumps(observed['result'], sort_keys=True, separators=(',', ':')) ==
                       json.dumps(self._receipt_json(operation['result']), sort_keys=True, separators=(',', ':')),
                       'terminal_receipt_sql_mismatch')
        except Exception:
            self._receipt_fault = 'terminal_receipt_recovery_required'

    def _prepare_receipt(self, payload, identity):
        from .terminal_receipt import TerminalReceipt, SLOT_BYTES
        if self._receipt_config is None:
            return
        root = self._receipt_config['path']
        ensure(self.db.execute('SELECT COUNT(*) FROM terminal_receipts').fetchone()[0] <
               self._receipt_config['max_slots'], 'terminal_receipt_slot_limit', 409)
        self._capacity(SLOT_BYTES)
        receipt = TerminalReceipt.prepare(root, payload['command_id'])
        binding = self._receipt_original(payload, identity)
        receipt.bind(binding)
        self.db.execute('INSERT INTO terminal_receipts VALUES (?,?,?,?,?)',
            (payload['command_id'], receipt.slot_id, str(root), json.dumps(receipt.capture(),sort_keys=True),
             json.dumps(binding,sort_keys=True)))

    def _terminal_receipt(self, command_id, state, result):
        from .terminal_receipt import TerminalReceipt
        if self._receipt_config is None:
            return
        row = self.db.execute('SELECT * FROM terminal_receipts WHERE command_id=?', (command_id,)).fetchone()
        ensure(row is not None and row['root'] == str(self._receipt_config['path']), 'terminal_receipt_missing')
        try:
            receipt = TerminalReceipt(row['root'], row['slot_id'], expected_capture=self._receipt_json(row['capture']))
            # No retained, independently bound backend runtime observation exists
            # here. Do not fabricate Engine/boot/invocation metadata.
            receipt.record_terminal(self._receipt_json(row['binding']), outcome=state, result=result, runtime=None)
        except Exception:
            # Retain known write/fsync failure across new reader instances. A
            # valid footer after a late fsync error is not durable success.
            if len(self._receipt_io_failed) < self._receipt_config['max_slots']:
                self._receipt_io_failed.add(command_id)
            else:
                self._receipt_io_failed_overflow = True
            raise

    def terminal_evidence(self, command_id):
        """Private original-ID evidence; never SQL reconciliation authority."""
        from .terminal_receipt import TerminalReceipt
        identifier(command_id)
        with self.lock:
            row = self.db.execute('SELECT * FROM terminal_receipts WHERE command_id=?', (command_id,)).fetchone()
            ensure(row is not None and self._receipt_config is not None and
                   row['root'] == str(self._receipt_config['path']), 'terminal_receipt_unavailable', 409)
            if command_id in self._receipt_io_failed or self._receipt_io_failed_overflow:
                return {'state': 'uncertain', 'reason': 'receipt_persistence_failed'}
            receipt = TerminalReceipt(row['root'], row['slot_id'], expected_capture=self._receipt_json(row['capture']))
            return receipt.evidence(self._receipt_json(row['binding']))

    def _capacity(self, extra):
        used = sum(item.stat().st_size for item in self.root.rglob('*') if item.is_file() and not item.is_symlink())
        ensure(used + extra <= self.capacity_bytes and shutil.disk_usage(self.root).free >= extra + self.free_floor_bytes,
               'worker_storage_capacity', 409)

    def put_source(self, payload):
        ensure(isinstance(payload, dict) and set(payload) == {'source_id', 'project', 'source_digest', 'source_zip_base64'},
               'source_schema')
        source_id = identifier(payload['source_id'])
        ensure(payload['project'] in self.projects, 'project_not_allowed', 403)
        raw = archive(payload['source_zip_base64'])
        digest = hashlib.sha256(raw).hexdigest()
        ensure(payload['source_digest'] == digest, 'source_digest_mismatch')
        extract(raw)  # Includes complete CRC/readback and expansion checks before deposit.
        with self.lock:
            row = self.db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
            if row:
                ensure(row['project'] == payload['project'] and row['digest'] == digest, 'source_identity_conflict', 409)
                self._source(source_id, payload['project'], digest)
            else:
                ensure(self._receipt_fault is None, 'terminal_receipt_recovery_required', 409)
                self.cleanup_sources()
                self._capacity(len(raw))
                path = self.sources / (source_id + '.zip')
                ensure(not path.exists() and not path.is_symlink(), 'source_path_conflict', 409)
                with path.open('xb') as stream:
                    stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                path.chmod(0o600)
                self.db.execute('INSERT INTO sources VALUES (?,?,?,?,?,?)',
                    (source_id, payload['project'], digest, len(raw), path.stat().st_ino, time.time()))
                self.db.commit()
            return {'source_id': source_id, 'source_digest': digest, 'size_bytes': len(raw), 'project': payload['project']}

    def _source(self, source_id, project, digest):
        identifier(source_id)
        row = self.db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
        ensure(row is not None and row['project'] == project and row['digest'] == digest, 'source_not_registered', 404)
        path = self.sources / (source_id + '.zip')
        ensure(not path.is_symlink() and path.stat().st_ino == row['inode'] and path.stat().st_size == row['bytes'],
               'source_identity_drift', 409)
        raw = path.read_bytes()
        ensure(hashlib.sha256(raw).hexdigest() == digest, 'source_identity_drift', 409)
        return raw

    def cleanup_sources(self, now=None):
        """Collect expired owned deposits after every referencing job is retired.

        Job/cleanup journals remain intact. Unknown, active, held or incomplete
        ownership facts are retention reasons, never permission to sweep paths.
        Called before new deposits; not part of recording a cleanup receipt.
        """
        with self.lock:
            ensure(self._receipt_fault is None, 'terminal_receipt_recovery_required', 409)
            references = {}
            for operation in self.db.execute('SELECT * FROM operations'):
                payload = json.loads(operation['payload'])
                source_id = payload.get('source_id')
                if source_id is not None:
                    references.setdefault(source_id, []).append((operation, payload))
            for row in self.db.execute('SELECT * FROM sources WHERE created<?', ((time.time() if now is None else now) - 86400,)).fetchall():
                if not all(self._source_job_retired(row, operation, payload)
                           for operation, payload in references.get(row['id'], [])):
                    continue
                path = self.sources / (row['id'] + '.zip')
                ensure(not path.is_symlink() and path.is_file() and path.stat().st_ino == row['inode'], 'source_cleanup_identity_drift', 409)
                path.unlink()
                self.db.execute('DELETE FROM sources WHERE id=?', (row['id'],))
                self.db.commit()

    def _source_job_retired(self, source, operation, payload):
        """Conservative same-journal proof; caller holds the operation lock."""
        if (operation['state'] not in TERMINAL or operation['job_id'] is None
                or self.active == operation['command_id']
                or payload.get('job_id') != operation['job_id']
                or payload.get('project') != source['project']
                or payload.get('source_digest') != source['digest']):
            return False
        wanted = payload.get('resource_ids')
        if (type(wanted) is not dict or set(wanted) != set(KINDS)
                or any(type(value) is not str for value in wanted.values())
                or len(set(wanted.values())) != 4):
            return False
        resources = self.db.execute('SELECT * FROM resources WHERE job_id=?', (operation['job_id'],)).fetchall()
        if len(resources) != 4 or {resource['kind'] for resource in resources} != set(KINDS):
            return False
        return all(resource['id'] == wanted[resource['kind']]
                   and resource['state'] == 'deleted' and json.loads(resource['refs']) == []
                   for resource in resources)

    def _operation(self, command_id, payload):
        identity = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        row = self.db.execute('SELECT * FROM operations WHERE command_id=?', (command_id,)).fetchone()
        if row:
            ensure(row['identity'] == identity, 'idempotency_conflict', 409)
            return identity, self._view(row)
        key = payload['idempotency_key']
        for old in self.db.execute('SELECT payload FROM operations'):
            ensure(json.loads(old[0])['idempotency_key'] != key, 'idempotency_conflict', 409)
        return identity, None

    def _view(self, row):
        return {'command_id': row['command_id'], 'job_id': row['job_id'], 'state': row['state'],
                'result': json.loads(row['result'])}

    def submit(self, payload):
        fields = {'job_id', 'command_id', 'idempotency_key', 'project', 'recipe', 'source_commit', 'source_digest', 'resource_ids'}
        ensure(isinstance(payload, dict) and set(payload) in (fields | {'source_id'}, fields | {'source_zip_base64'}), 'job_schema')
        identifier(payload['job_id']); identifier(payload['command_id'])
        ensure(isinstance(payload['idempotency_key'], str) and 0 < len(payload['idempotency_key']) <= 128,
               'invalid_idempotency_key')
        ensure(payload['project'] in self.projects, 'project_not_allowed', 403)
        ensure(payload['recipe'] in self.projects[payload['project']], 'recipe_not_allowed', 403)
        profile = self.projects[payload['project']][payload['recipe']]
        ensure(re.fullmatch(r'[0-9a-f]{40}', payload.get('source_commit', '')) is not None, 'invalid_commit')
        ensure(re.fullmatch(r'[0-9a-f]{64}', payload.get('source_digest', '')) is not None, 'invalid_source_digest')
        ensure(isinstance(payload['resource_ids'], dict) and set(payload['resource_ids']) == set(KINDS), 'resource_schema')
        ids = [identifier(payload['resource_ids'][kind]) for kind in KINDS]
        ensure(len(set(ids)) == len(ids), 'duplicate_resource_id')
        with self.lock:
            identity, existing = self._operation(payload['command_id'], payload)
            if existing:
                return existing
            raw = self._source(payload['source_id'], payload['project'], payload['source_digest']) if 'source_id' in payload else archive(payload['source_zip_base64'])
            ensure(hashlib.sha256(raw).hexdigest() == payload['source_digest'], 'source_digest_mismatch')
            extract(raw)
            ensure(self.active is None and not self.db.execute("SELECT 1 FROM operations WHERE state IN ('held','running','accepted')").fetchone(),
                   'worker_busy_or_held', 409)
            self._receipt_inventory()
            ensure(self._receipt_fault is None, 'terminal_receipt_recovery_required', 409)
            ensure(not self.db.execute('SELECT 1 FROM operations WHERE job_id=?', (payload['job_id'],)).fetchone(), 'job_conflict', 409)
            for resource_id in ids:
                ensure(not self.db.execute('SELECT 1 FROM resources WHERE id=?', (resource_id,)).fetchone()
                       and not (self.resources / resource_id).exists(), 'resource_conflict', 409)
            reservation = MAX_EXPANDED + 3 * profile['output_bytes'] + len(raw)
            if self._receipt_config is not None:
                from .terminal_receipt import SLOT_BYTES
                reservation += SLOT_BYTES
            self._capacity(reservation)
            # The full incoming payload is already bound by `identity`. Retain
            # compact provenance/idempotency facts, not an inline archive forever.
            persisted = {key: value for key, value in payload.items() if key != 'source_zip_base64'}
            try:
                self.db.execute('BEGIN IMMEDIATE')
                ensure(not self.db.execute("SELECT 1 FROM scratch_leases WHERE state!='released'").fetchone(),
                       'scratch_occupied', 409)
                if self.scratch_slot is not None:
                    self.scratch_slot.reserve(self.db, payload['command_id'], payload['job_id'], payload['resource_ids'])
                self._prepare_receipt(payload, identity)
                self.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',
                                (payload['command_id'], identity, payload['job_id'], 'accepted', json.dumps(persisted), '{}'))
                self.db.commit()
            except Exception:
                if self._receipt_config is not None:
                    self._receipt_fault = 'terminal_receipt_persistence_failed'
                self.db.rollback()
                raise
            self.active = payload['command_id']
            self.thread = threading.Thread(target=self._execute, args=(payload, raw), daemon=False)
            try:
                self.thread.start()
            except Exception:
                if self._receipt_config is not None:
                    self._receipt_fault = 'terminal_receipt_recovery_required'
                try:
                    self.db.execute("UPDATE operations SET state='held' WHERE command_id=?", (self.active,))
                    self.db.commit()
                finally:
                    self.active = None
                    self.thread = None
                raise Rejected('worker_start_failed', 503) from None
            return self.status(payload['job_id'])

    def _execute(self, payload, raw):
        try:
            self._execute_operation(payload, raw)
        except Exception:
            # Preserve the original reservation even if SQLite cannot record
            # failure. The sticky veto and startup accepted/running->held gate
            # prevent another job; private receipt remains evidence only.
            if self._receipt_config is not None:
                self._receipt_fault = 'terminal_receipt_persistence_failed'
            with self.lock:
                try:
                    self.db.rollback()
                    self.db.execute("UPDATE operations SET state='held' WHERE command_id=?", (payload['command_id'],))
                    self.db.commit()
                except Exception:
                    try: self.db.rollback()
                    except Exception: pass
        finally:
            with self.lock:
                self.active = None

    def _execute_operation(self, payload, raw):
        lease = None
        try:
            with self.lock:
                self.db.execute("UPDATE operations SET state='running' WHERE command_id=?", (payload['command_id'],))
                self.db.commit()
                if self.scratch_slot is not None:
                    lease = self.scratch_slot.lease(self.db, payload['command_id'])
                    lease.materialize()
            paths = {}
            for kind, resource_id in payload['resource_ids'].items():
                path = self.resources / resource_id
                path.mkdir(mode=0o700)
                paths[kind] = path
                with self.lock:
                    self.db.execute('INSERT INTO resources VALUES (?,?,?,?,?,?,?,?,?)',
                        (resource_id, payload['job_id'], kind, 1, 'building', path.stat().st_ino, '{}', 0, '[]'))
                    self.db.commit()
            extract(raw, paths['workspace'])
            make_job_readable(paths['workspace'], MAX_EXPANDED)
            profile = self.projects[payload['project']][payload['recipe']]
            if lease is None:
                result = self.backend.run(paths, profile, payload['command_id'])
            else:
                result = self.backend.run(paths, profile, payload['command_id'], scratch_lease=lease)
                ensure(lease.backend_absent, 'execution_uncertain', 409)
            for kind, path in paths.items():
                limit = MAX_EXPANDED if kind == 'workspace' else profile['output_bytes']
                manifest, size = tree_manifest(path, limit)
                with self.lock:
                    self.db.execute("UPDATE resources SET state='ready',manifest=?,bytes=? WHERE id=?",
                                    (json.dumps(manifest), size, payload['resource_ids'][kind]))
                    self.db.commit()
            result.update(source_commit=payload['source_commit'], source_commit_verification='declared',
                          source_digest=payload['source_digest'], resource_ids=payload['resource_ids'])
            state = 'succeeded'
        except Exception as exc:
            from .docker import DockerBackend, FrozenDiagnosticFailure
            if type(exc) is FrozenDiagnosticFailure:
                ensure(type(self.backend) is DockerBackend and exc.command_id == payload['command_id']
                       and exc.resource_id == payload['resource_ids']['test_run']
                       and exc.stage in ('build', 'test') and type(exc.engine_id) is str
                       and re.fullmatch('[A-Za-z0-9:_-]{1,128}', exc.engine_id), 'execution_uncertain')
                if self.scratch_slot is not None:
                    ensure(lease is not None and lease.backend_absent, 'execution_uncertain')
                path = paths['test_run']
                manifest, size = tree_manifest(path, profile['output_bytes'])
                ensure(manifest == exc.manifest and size == exc.size and bool(manifest), 'execution_uncertain')
                with self.lock:
                    row = self.db.execute('SELECT * FROM resources WHERE id=?', (exc.resource_id,)).fetchone()
                    ensure(row is not None and row['job_id'] == payload['job_id'] and row['kind'] == 'test_run'
                           and row['state'] == 'building' and not path.is_symlink()
                           and path.stat().st_ino == row['inode'], 'execution_uncertain')
                    self.db.execute("UPDATE resources SET state='ready',manifest=?,bytes=? WHERE id=?",
                                    (json.dumps(manifest), size, exc.resource_id))
                    self.db.commit()
                result = {'error': exc.code, 'diagnostic': {'resource_id': exc.resource_id,
                    'manifest_sha256': hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',', ':')).encode()).hexdigest(),
                    'bytes': size, 'stage': exc.stage, 'scope': 'original_engine_owned_writers_exporters_volumes_absent'}}
                state = 'failed'
            else:
                result = {'error': exc.code if isinstance(exc, Rejected) else 'execution_uncertain'}
                state = 'held' if result['error'] == 'execution_uncertain' else 'failed'
            if self._receipt_config is not None and isinstance(exc, (sqlite3.Error, OSError)):
                self._receipt_fault = 'terminal_receipt_persistence_failed'
        if self.scratch_slot is not None and (lease is None or not lease.backend_absent):
            state = 'held'
            result = {'error': 'scratch_cleanup_unproven'}
        with self.lock:
            self._terminal_receipt(payload['command_id'], state, result)
            if lease is not None and lease.backend_absent:
                self.db.execute('UPDATE scratch_leases SET absence=? WHERE command_id=?',
                                (json.dumps(lease.absence_receipt(), sort_keys=True), payload['command_id']))
            self.db.execute('UPDATE operations SET state=?,result=? WHERE command_id=?',
                            (state, json.dumps(result), payload['command_id']))
            self.db.commit()
            if lease is not None and state in TERMINAL:
                try:
                    lease.release()
                except Exception:
                    if self._receipt_config is not None:
                        self._receipt_fault = 'terminal_receipt_recovery_required'
                    self.db.rollback()
                    self.db.execute("UPDATE operations SET state='held',result=? WHERE command_id=?",
                                    ('{"error":"scratch_cleanup_uncertain"}', payload['command_id']))
                    self.db.commit()
            self.active = None

    def status(self, job_id):
        identifier(job_id)
        with self.lock:
            row = self.db.execute('SELECT * FROM operations WHERE job_id=?', (job_id,)).fetchone()
            ensure(row is not None, 'job_not_found', 404)
            return self._view(row)

    def operation(self, command_id):
        """Read a previously issued cleanup outcome; never reapply its effect."""
        identifier(command_id)
        with self.lock:
            row = self.db.execute('SELECT * FROM operations WHERE command_id=?', (command_id,)).fetchone()
            ensure(row is not None, 'operation_not_found', 404)
            ensure(row['job_id'] is None, 'operation_not_cleanup', 409)
            return self._view(row)

    def metadata(self, resource_id):
        identifier(resource_id)
        with self.lock:
            row = self.db.execute('SELECT * FROM resources WHERE id=?', (resource_id,)).fetchone()
            ensure(row is not None, 'resource_not_found', 404)
            return {**dict(row), 'manifest': json.loads(row['manifest']), 'refs': json.loads(row['refs'])}

    def file(self, resource_id, name):
        relative(name)
        with self.lock:
            item = self.metadata(resource_id)
            ensure(item['kind'] == 'preview' and item['state'] == 'ready', 'resource_not_preview', 409)
            ensure(name in item['manifest'], 'file_not_registered', 404)
            path = self.resources / resource_id
            ensure(not path.is_symlink() and path.stat().st_ino == item['inode'], 'resource_identity_drift', 409)
            for part in relative(name).parts:
                path = path / part
                ensure(not path.is_symlink(), 'resource_symlink', 409)
            data = path.read_bytes()
            ensure(len(data) == item['manifest'][name]['bytes'] and hashlib.sha256(data).hexdigest() ==
                   item['manifest'][name]['sha256'], 'artifact_changed', 409)
            return data

    def diagnostic_file(self, resource_id, manifest_sha256, name):
        """Only registered, terminal-failed test_run bytes; never inline HTML."""
        identifier(resource_id); relative(name)
        ensure(type(manifest_sha256) is str and re.fullmatch('[0-9a-f]{64}', manifest_sha256), 'diagnostic_binding_invalid')
        ensure(os.name == 'posix', 'diagnostic_posix_required')
        with self.lock:
            item = self.metadata(resource_id)
            ensure(item['kind'] == 'test_run' and item['state'] == 'ready', 'diagnostic_not_registered', 409)
            row = self.db.execute('SELECT * FROM operations WHERE job_id=?', (item['job_id'],)).fetchone()
            ensure(row is not None and row['state'] == 'failed', 'diagnostic_not_terminal', 409)
            result = json.loads(row['result']); saved = result.get('diagnostic', {})
            digest = hashlib.sha256(json.dumps(item['manifest'],sort_keys=True,separators=(',', ':')).encode()).hexdigest()
            ensure(saved.get('resource_id') == resource_id and saved.get('manifest_sha256') == digest == manifest_sha256
                   and saved.get('bytes') == item['bytes'] and name in item['manifest'], 'diagnostic_binding_invalid', 409)
            entry = item['manifest'][name]
            ensure(type(entry['bytes']) is int and 0 <= entry['bytes'] <= 8*1024**2, 'diagnostic_file_too_large', 409)
            directory = os.open(self.resources, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            fd = None
            try:
                current = os.open(resource_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory);directory=current
                ensure(os.fstat(directory).st_ino == item['inode'], 'resource_identity_drift', 409)
                parts = relative(name).parts
                for part in parts[:-1]:
                    current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    os.close(directory);directory=current
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                before = os.fstat(fd)
                ensure(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size == entry['bytes'],
                       'artifact_changed', 409)
                with os.fdopen(fd, 'rb') as stream:
                    fd = None; data = stream.read(entry['bytes']+1); after=os.fstat(stream.fileno())
                ensure((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns) ==
                       (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)
                       and len(data) == entry['bytes'] and hashlib.sha256(data).hexdigest() == entry['sha256'],
                       'artifact_changed', 409)
                return data
            finally:
                if fd is not None:os.close(fd)
                os.close(directory)

    def cleanup(self, payload):
        # Job receipts do not cover cleanup commands. Their existing SQL/CAS
        # authority remains separate; a known persistence fault vetoes all NEW
        # cleanup effects rather than pretending the job receipt covers them.
        ensure(isinstance(payload, dict) and set(payload) == {'command_id', 'idempotency_key', 'resources'}, 'cleanup_schema')
        identifier(payload['command_id'])
        ensure(isinstance(payload['idempotency_key'], str) and 0 < len(payload['idempotency_key']) <= 128,
               'invalid_idempotency_key')
        ensure(isinstance(payload['resources'], list) and 0 < len(payload['resources']) <= 4, 'cleanup_resources')
        with self.lock:
            identity, existing = self._operation(payload['command_id'], payload)
            if existing:
                return existing
            ensure(self._receipt_fault is None, 'terminal_receipt_recovery_required', 409)
            ensure(self.active is None, 'worker_busy', 409)
            items = []
            for wanted in payload['resources']:
                ensure(isinstance(wanted, dict) and set(wanted) == {'resource_id', 'expected_revision'}, 'cleanup_resource_schema')
                item = self.metadata(wanted['resource_id'])
                ensure(item['state'] != 'deleted' and type(wanted['expected_revision']) is int and
                       wanted['expected_revision'] == item['revision'] and not item['refs'], 'cleanup_revision_or_reference', 409)
                ensure(self.status(item['job_id'])['state'] in TERMINAL, 'resource_job_not_terminal', 409)
                path = self.resources / item['id']
                ensure(path.parent == self.resources and not path.is_symlink() and path.is_dir() and
                       path.stat().st_ino == item['inode'], 'cleanup_scope_drift', 409)
                tree_manifest(path, MAX_EXPANDED)
                items.append(item)
            ensure(len({x['id'] for x in items}) == len(items), 'duplicate_cleanup_resource')
            self.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',
                            (payload['command_id'], identity, None, 'running', json.dumps(payload), '{}'))
            self.db.commit()
            for item in items:
                try:
                    path = self.resources / item['id']
                    # Lock spans revalidation and deletion; never traverse unknown paths.
                    ensure(not path.is_symlink() and path.stat().st_ino == item['inode'], 'cleanup_scope_drift', 409)
                    tree_manifest(path, MAX_EXPANDED)
                    self.db.execute("UPDATE resources SET state='deleting' WHERE id=?", (item['id'],))
                    self.db.commit()
                    shutil.rmtree(path)
                    self.db.execute("UPDATE resources SET state='deleted',revision=revision+1,manifest='{}',bytes=0 WHERE id=?", (item['id'],))
                    self.db.commit()
                except Exception:
                    self.db.execute("UPDATE resources SET state='delete_failed' WHERE id=?", (item['id'],))
                    self.db.execute("UPDATE operations SET state='held',result=? WHERE command_id=?",
                                    ('{"error":"cleanup_uncertain"}', payload['command_id']))
                    self.db.commit()
                    raise Rejected('cleanup_uncertain', 409) from None
            result = {'deleted_resource_ids': [x['id'] for x in items]}
            self.db.execute("UPDATE operations SET state='succeeded',result=? WHERE command_id=?",
                            (json.dumps(result), payload['command_id']))
            self.db.commit()
            return {'command_id': payload['command_id'], 'job_id': None, 'state': 'succeeded', 'result': result}

    def wait(self):
        if self.thread:
            self.thread.join()

    def close(self):
        self.wait()
        self.db.close()
        self.process_lock.close()
        if self._scratch_lock is not None:
            os.close(self._scratch_lock)
            self._scratch_lock = None
