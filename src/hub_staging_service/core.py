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

# Independent development interlock for direct worker POSTs. Existing status
# and recovery reads remain available. Do not enable before protocol review.
_PROFILE_PROTOCOL_READY = False
PROFILE_PROTOCOL = 'hub-staging-profile.v2'

MAX_ARCHIVE = 8 * 1024 * 1024
# This observation is not an authenticated ledger epoch or authority to retry.
_SOURCE_EVIDENCE_ENABLED = False
# Experimental receiver record only; does not confer worker or epoch authority.
_SOURCE_ATTEMPT_ENABLED = False
_SOURCE_ATTEMPT_LIMIT = 4096
_SOURCE_ATTEMPT_INTERNAL = object()  # Same-process routing, not a security capability.
_SOURCE_ATTEMPT_DDL = ("CREATE TABLE source_attempts (source_id TEXT PRIMARY KEY, "
                       "project TEXT NOT NULL, digest TEXT NOT NULL, binding TEXT NOT NULL, "
                       "state TEXT NOT NULL, source_inode INTEGER, source_created REAL)")
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


def make_job_readable(path, limit, *, executable_paths=frozenset()):
    """Validate an owned immutable tree before exposing it via a read-only bind.

    Rootless Docker maps the host owner to container root; the configured job
    UID therefore needs read/traverse bits. Only this resource tree changes;
    its private worker/resource ancestors remain inaccessible to other hosts.
    """
    manifest, _ = tree_manifest(path, limit)
    ensure(type(executable_paths) is frozenset and executable_paths <= manifest.keys(),
           'invalid_source_executable_paths')
    for directory, dirs, files in os.walk(path, followlinks=False):
        Path(directory).chmod(0o755)
        for name in files:
            item = Path(directory) / name
            item.chmod(0o755 if item.relative_to(path).as_posix() in executable_paths else 0o644)


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
            CREATE TABLE IF NOT EXISTS source_bindings (
                source_id TEXT PRIMARY KEY, source_inode INTEGER NOT NULL,
                source_created REAL NOT NULL, binding TEXT NOT NULL);
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
            if self.db.execute("SELECT 1 FROM operations WHERE job_id IS NULL AND state='held' LIMIT 1").fetchone():
                self._receipt_fault = 'cleanup_recovery_required'
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

    def _receipt_worker(self):
        journal = (self.root / 'journal.sqlite3').stat(follow_symlinks=False)
        root = self.root.stat(follow_symlinks=False)
        worker = str(uuid.uuid5(uuid.NAMESPACE_URL, 'engineering-hub-worker:' +
            json.dumps([[root.st_dev, root.st_ino], [journal.st_dev, journal.st_ino]])))
        return worker, [journal.st_dev, journal.st_ino]

    def _receipt_original(self, payload, identity):
        worker, journal = self._receipt_worker()
        lease = self.db.execute('SELECT capture FROM scratch_leases WHERE command_id=?',
                                (payload['command_id'],)).fetchone()
        return {'command_id': payload['command_id'], 'job_id': payload['job_id'],
                'operation_identity': identity, 'worker_id': worker,
                'journal_identity': journal,
                'lease_identity': hashlib.sha256(lease['capture'].encode()).hexdigest() if lease else None}

    def _cleanup_binding(self, payload, identity, items, root_identity):
        worker, journal = self._receipt_worker()
        return {'operation_kind': 'cleanup', 'command_id': payload['command_id'], 'job_id': None,
                'operation_identity': identity, 'worker_id': worker, 'journal_identity': journal,
                'lease_identity': None, 'resource_root_identity': list(root_identity),
                'resources': [{'resource_id': item['id'], 'job_id': item['job_id'], 'kind': item['kind'],
                    'revision': item['revision'], 'inode': item['inode'],
                    'manifest_sha256': item['_observed_manifest_sha256']} for item in items]}

    def _validate_cleanup_binding(self, binding, operation):
        from .terminal_receipt import _binding
        binding = _binding(binding)
        payload = self._receipt_json(operation['payload'])
        ensure(binding.get('operation_kind') == 'cleanup' and operation['job_id'] is None
               and type(payload) is dict and set(payload) == {'command_id', 'idempotency_key', 'resources'}
               and payload['command_id'] == operation['command_id'] == binding['command_id']
               and binding['operation_identity'] == operation['identity']
               and hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',', ':')).encode()).hexdigest() == operation['identity']
               and type(payload['resources']) is list and len(payload['resources']) == len(binding['resources']),
               'terminal_receipt_binding_changed')
        parent = self.resources.stat(follow_symlinks=False)
        worker, journal = self._receipt_worker()
        ensure(binding['worker_id'] == worker and binding['journal_identity'] == journal
               and binding['resource_root_identity'] == [parent.st_dev, parent.st_ino], 'terminal_receipt_binding_changed')
        for wanted, captured in zip(payload['resources'], binding['resources']):
            ensure(type(wanted) is dict and set(wanted) == {'resource_id', 'expected_revision'}
                   and wanted['resource_id'] == captured['resource_id']
                   and type(wanted['expected_revision']) is int and wanted['expected_revision'] == captured['revision'],
                   'terminal_receipt_binding_changed')
            row = self.db.execute('SELECT * FROM resources WHERE id=?', (captured['resource_id'],)).fetchone()
            ensure(row is not None and row['job_id'] == captured['job_id'] and row['kind'] == captured['kind']
                   and row['inode'] == captured['inode'], 'terminal_receipt_binding_changed')
            if operation['state'] == 'succeeded':
                ensure(row['state'] == 'deleted' and row['revision'] == captured['revision']+1
                       and self._receipt_json(row['manifest']) == {} and row['bytes'] == 0
                       and self._receipt_json(row['refs']) == []
                       and not (self.resources / row['id']).exists() and not (self.resources / row['id']).is_symlink(),
                       'terminal_receipt_cleanup_mismatch')
        if operation['state'] == 'succeeded':
            ensure(self._receipt_json(operation['result']) ==
                   {'deleted_resource_ids': [item['resource_id'] for item in binding['resources']]},
                   'terminal_receipt_cleanup_mismatch')

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
                ensure(operation is not None and binding['command_id'] == row['command_id']
                       and binding['operation_identity'] == operation['identity'], 'terminal_receipt_binding_changed')
                if binding.get('operation_kind') == 'cleanup':
                    self._validate_cleanup_binding(binding, operation)
                else:
                    ensure(operation['job_id'] is not None and binding['job_id'] == operation['job_id'],
                           'terminal_receipt_binding_changed')
                    # Original job intent predates materialization; never rebind.
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

    def _prepare_receipt(self, payload, identity, *, binding=None):
        from .terminal_receipt import TerminalReceipt, SLOT_BYTES
        if self._receipt_config is None:
            return
        root = self._receipt_config['path']
        ensure(self.db.execute('SELECT COUNT(*) FROM terminal_receipts').fetchone()[0] <
               self._receipt_config['max_slots'], 'terminal_receipt_slot_limit', 409)
        self._capacity(SLOT_BYTES)
        receipt = TerminalReceipt.prepare(root, payload['command_id'])
        binding = self._receipt_original(payload, identity) if binding is None else binding
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

    def _legacy_put_source(self, payload, *, _attempt_owner=None):
        fields = {'source_id', 'project', 'source_digest', 'source_zip_base64'}
        ensure(isinstance(payload, dict) and set(payload) in
               (fields, fields | {'source_commit', 'source_commit_object_base64'}), 'source_schema')
        source_id = identifier(payload['source_id'])
        ensure(payload['project'] in self.projects, 'project_not_allowed', 403)
        raw = archive(payload['source_zip_base64'])
        digest = hashlib.sha256(raw).hexdigest()
        ensure(payload['source_digest'] == digest, 'source_digest_mismatch')
        extract(raw)  # Includes complete CRC/readback and expansion checks before deposit.
        binding = None
        if 'source_commit' in payload:
            from .source_provenance import verify
            try:
                binding = verify(raw, payload['source_commit_object_base64'], payload['source_commit'])
            except ValueError:
                raise Rejected('source_binding_invalid') from None
        binding_json = json.dumps(binding, sort_keys=True, separators=(',', ':')) if binding is not None else None
        with self.lock:
            row = self.db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
            if row:
                # Keep the original legacy retry path when the experimental
                # table does not exist. A separate process introducing tables
                # concurrently is outside the single-owner worker contract.
                attempt_schema = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='source_attempts'").fetchone()
                if attempt_schema is None and _attempt_owner is None:
                    ensure(row['project'] == payload['project'] and row['digest'] == digest
                           and self._source_binding(source_id, payload['project'], digest) == binding,
                           'source_identity_conflict', 409)
                    self._source(source_id, payload['project'], digest)
                else:
                    self.db.execute('BEGIN IMMEDIATE')
                    try:
                        self._guard_source_attempt(source_id, payload['project'], digest, binding_json, _attempt_owner)
                        ensure(_attempt_owner is None, 'source_attempt_conflict', 409)
                        row = self.db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
                        ensure(row is not None and row['project'] == payload['project'] and row['digest'] == digest,
                               'source_identity_conflict', 409)
                        prior = self.db.execute('SELECT source_inode,source_created FROM source_bindings WHERE source_id=?',
                                                (source_id,)).fetchone()
                        # _source_binding deletes and commits a stale binding.
                        # Never let it end the attempt guard's writer early.
                        ensure(prior is None or (prior['source_inode'] == row['inode'] and prior['source_created'] == row['created']),
                               'source_identity_conflict', 409)
                        ensure(self._source_binding(source_id, payload['project'], digest) == binding,
                               'source_identity_conflict', 409)
                        self._source(source_id, payload['project'], digest)
                        self.db.commit()
                    except Exception:
                        self.db.rollback()
                        raise
            else:
                ensure(self._receipt_fault is None, 'terminal_receipt_recovery_required', 409)
                # Check before cleanup as well as at the effect transaction:
                # a reserved ID must not cause unrelated source collection.
                self.db.execute('BEGIN IMMEDIATE')
                try:
                    self._guard_source_attempt(source_id, payload['project'], digest, binding_json, _attempt_owner)
                    self.db.commit()
                except Exception:
                    self.db.rollback()
                    raise
                self.cleanup_sources()
                self._capacity(len(raw))
                path = self.sources / (source_id + '.zip')
                ensure(not path.exists() and not path.is_symlink(), 'source_path_conflict', 409)
                created_inode = None
                self.db.execute('BEGIN IMMEDIATE')
                try:
                    self._guard_source_attempt(source_id, payload['project'], digest, binding_json, _attempt_owner)
                    if _attempt_owner is _SOURCE_ATTEMPT_INTERNAL:
                        ensure(self.db.execute('SELECT 1 FROM source_bindings WHERE source_id=?', (source_id,)).fetchone() is None,
                               'source_attempt_conflict', 409)
                    # A previous worker does not enable SQLite foreign keys,
                    # so its exact legacy DELETE may leave this one orphan.
                    # Absence of the source row makes this binding stale.
                    self.db.execute('DELETE FROM source_bindings WHERE source_id=?', (source_id,))
                    with path.open('xb') as stream:
                        created_inode = os.fstat(stream.fileno()).st_ino
                        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                    path.chmod(0o600)
                    created_at = time.time()
                    self.db.execute('INSERT INTO sources VALUES (?,?,?,?,?,?)',
                        (source_id, payload['project'], digest, len(raw), created_inode, created_at))
                    if binding_json is not None:
                        self.db.execute('INSERT INTO source_bindings '
                            '(source_id,source_inode,source_created,binding) VALUES (?,?,?,?)',
                            (source_id, created_inode, created_at, binding_json))
                    self.db.commit()
                except Exception:
                    self.db.rollback()
                    if (created_inode is not None and not path.is_symlink() and path.exists()
                            and path.stat().st_ino == created_inode):
                        path.unlink()
                    raise
            result = {'source_id': source_id, 'source_digest': digest, 'size_bytes': len(raw),
                      'project': payload['project']}
            if binding is not None:
                result['source_binding'] = binding
            return result


    def put_source(self, payload):
        if isinstance(payload, dict) and 'profile_protocol' in payload:
            return self._put_source_v2(payload)
        return self._legacy_put_source(payload)

    def _put_source_v2(self, payload):
        ensure(_PROFILE_PROTOCOL_READY, 'profile_protocol_unavailable', 503)
        fields = {'source_id', 'project', 'source_digest', 'source_zip_base64',
                  'source_commit', 'source_commit_object_base64', 'profile_protocol'}
        ensure(type(payload) is dict and set(payload) == fields
               and payload['profile_protocol'] == PROFILE_PROTOCOL, 'source_schema')
        with self.lock:
            ensure(_PROFILE_PROTOCOL_READY, 'profile_protocol_unavailable', 503)
            return self.put_source_attempt({key: value for key, value in payload.items() if key != 'profile_protocol'})

    def _source_attempt_schema(self):
        """Validate the experimental journal before trusting its contents."""
        row = self.db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='source_attempts'").fetchone()
        if row is None:
            self.db.execute(_SOURCE_ATTEMPT_DDL)
            row = self.db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='source_attempts'").fetchone()
        ensure(row is not None and row['sql'] == _SOURCE_ATTEMPT_DDL, 'source_attempt_journal_invalid', 503)
        extras = self.db.execute("SELECT type FROM sqlite_master WHERE tbl_name='source_attempts' "
                                 "AND type IN ('trigger','index') AND name NOT LIKE 'sqlite_autoindex_%'").fetchall()
        ensure(not extras, 'source_attempt_journal_invalid', 503)

    def _guard_source_attempt(self, source_id, project, digest, binding_json, owner):
        """Called under the effect writer transaction; v1 cannot adopt v2 IDs."""
        objects = self.db.execute("SELECT type FROM sqlite_master WHERE name='source_attempts'").fetchall()
        if not objects:
            ensure(owner is None, 'source_attempt_journal_invalid', 503)
            return False
        ensure(len(objects) == 1 and objects[0]['type'] == 'table', 'source_attempt_journal_invalid', 503)
        self._source_attempt_schema()
        row = self.db.execute('SELECT * FROM source_attempts WHERE source_id=?', (source_id,)).fetchone()
        if owner is None:
            ensure(row is None, 'source_attempt_reserved', 409)
        else:
            ensure(owner is _SOURCE_ATTEMPT_INTERNAL and row is not None and
                   (row['project'], row['digest'], row['binding'], row['state']) ==
                   (project, digest, binding_json, 'uncertain'), 'source_attempt_journal_invalid', 503)
        return True

    def _require_observed_source_attempt(self, payload):
        """Admission check only; does not prove peer epoch or host fencing."""
        objects = self.db.execute("SELECT type FROM sqlite_master WHERE name='source_attempts'").fetchall()
        ensure(len(objects) == 1 and objects[0]['type'] == 'table', 'source_attempt_required', 409)
        self._source_attempt_schema()
        attempt = self.db.execute('SELECT * FROM source_attempts WHERE source_id=?', (payload['source_id'],)).fetchone()
        source = self.db.execute('SELECT * FROM sources WHERE id=?', (payload['source_id'],)).fetchone()
        binding = self.db.execute('SELECT * FROM source_bindings WHERE source_id=?', (payload['source_id'],)).fetchone()
        canonical = json.dumps(payload['expected_source_binding'], sort_keys=True, separators=(',', ':'))
        ensure(attempt is not None and source is not None and binding is not None
               and (attempt['project'], attempt['digest'], attempt['binding'], attempt['state']) ==
                   (payload['project'], payload['source_digest'], canonical, 'historical_observation')
               and (source['project'], source['digest'], source['inode'], source['created']) ==
                   (payload['project'], payload['source_digest'], attempt['source_inode'], attempt['source_created'])
               and (binding['binding'], binding['source_inode'], binding['source_created']) ==
                   (canonical, source['inode'], source['created']), 'source_attempt_required', 409)

    def put_source_attempt(self, payload):
        """Record an original attempt before an experimental deposit; never replay it.

        The persistent record is evidence only. It does not authenticate this
        process, exclude other writers, authorize a hold release or prove that
        an absent attempt had no effect. Unknown states are retained indefinitely.
        """
        ensure(_SOURCE_ATTEMPT_ENABLED and _PROFILE_PROTOCOL_READY, 'source_attempt_unavailable', 503)
        fields = {'source_id', 'project', 'source_digest', 'source_zip_base64',
                  'source_commit', 'source_commit_object_base64'}
        ensure(type(payload) is dict and set(payload) == fields, 'source_attempt_schema')
        source_id = identifier(payload['source_id'])
        ensure(type(payload['project']) is str and payload['project'] in self.projects, 'project_not_allowed', 403)
        raw = archive(payload['source_zip_base64'])
        digest = hashlib.sha256(raw).hexdigest()
        ensure(payload['source_digest'] == digest, 'source_digest_mismatch')
        extract(raw)
        from .source_provenance import verify
        try:
            binding = verify(raw, payload['source_commit_object_base64'], payload['source_commit'])
        except ValueError:
            raise Rejected('source_binding_invalid') from None
        canonical = json.dumps(binding, sort_keys=True, separators=(',', ':'))
        with self.lock:
            ensure(_SOURCE_ATTEMPT_ENABLED and _PROFILE_PROTOCOL_READY, 'source_attempt_unavailable', 503)
            # Existing terminal-receipt uncertainty is a veto on every new
            # mutation, including the pre-effect attempt record itself.
            ensure(self._receipt_fault is None, 'terminal_receipt_recovery_required', 409)
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self._source_attempt_schema()
                previous = self.db.execute('SELECT * FROM source_attempts WHERE source_id=?', (source_id,)).fetchone()
                if previous is not None:
                    ensure((previous['project'], previous['digest'], previous['binding']) ==
                           (payload['project'], digest, canonical), 'source_attempt_conflict', 409)
                    # Even a completed response is not replayed or presented as
                    # live worker authority. Recovery must inspect the original.
                    raise Rejected('source_attempt_unknown', 409)
                ensure(self.db.execute('SELECT COUNT(*) FROM source_attempts').fetchone()[0] < _SOURCE_ATTEMPT_LIMIT,
                       'source_attempt_capacity', 409)
                ensure(self.db.execute('SELECT 1 FROM sources WHERE id=?', (source_id,)).fetchone() is None
                       and self.db.execute('SELECT 1 FROM source_bindings WHERE source_id=?', (source_id,)).fetchone() is None
                       and not (self.sources / (source_id + '.zip')).exists()
                       and not (self.sources / (source_id + '.zip')).is_symlink(), 'source_attempt_conflict', 409)
                self.db.execute('INSERT INTO source_attempts VALUES(?,?,?,?,?,?,?)',
                                (source_id, payload['project'], digest, canonical, 'uncertain', None, None))
                stored = self.db.execute('SELECT * FROM source_attempts WHERE source_id=?', (source_id,)).fetchone()
                ensure(stored is not None and tuple(stored) == (source_id, payload['project'], digest, canonical, 'uncertain', None, None),
                       'source_attempt_journal_invalid', 503)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            # Recheck even while holding the worker's process-local lock. A
            # separate DB writer is outside this prototype's trust boundary.
            ensure(self.db.execute('SELECT 1 FROM source_bindings WHERE source_id=?', (source_id,)).fetchone() is None,
                   'source_attempt_conflict', 409)
            receipt = self._legacy_put_source(payload, _attempt_owner=_SOURCE_ATTEMPT_INTERNAL)
            # Confirming the local effect is not proof of process identity or
            # external quiescence. Failure here leaves the original uncertain.
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self._source_attempt_schema()
                source = self.db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
                source_binding = self.db.execute('SELECT * FROM source_bindings WHERE source_id=?', (source_id,)).fetchone()
                ensure(source is not None and source_binding is not None and source['project'] == payload['project']
                       and source['digest'] == digest and source_binding['binding'] == canonical
                       and source_binding['source_inode'] == source['inode'] and source_binding['source_created'] == source['created'],
                       'source_attempt_conflict', 409)
                changed = self.db.execute("UPDATE source_attempts SET state='historical_observation',source_inode=?,source_created=? WHERE source_id=? "
                    "AND project=? AND digest=? AND binding=? AND state='uncertain'",
                    (source['inode'], source['created'], source_id, payload['project'], digest, canonical)).rowcount
                ensure(changed == 1, 'source_attempt_journal_invalid', 503)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            return {'authority': 'observation_only', 'state': 'historical_observation', **receipt}

    def source_attempt(self, source_id, project):
        """Read retained local attempt metadata without authorizing any action."""
        ensure(_SOURCE_ATTEMPT_ENABLED, 'source_attempt_unavailable', 503)
        identifier(source_id)
        ensure(type(project) is str and project in self.projects, 'project_not_allowed', 403)
        with self.lock:
            # Read-only GET does not create a schema for a never-used feature.
            row = self.db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='source_attempts'").fetchone()
            if row is None:
                return {'state': 'unknown', 'authority': 'observation_only'}
            self._source_attempt_schema()
            row = self.db.execute('SELECT * FROM source_attempts WHERE source_id=?', (source_id,)).fetchone()
            if row is None or row['project'] != project or row['state'] not in ('uncertain', 'historical_observation'):
                return {'state': 'unknown', 'authority': 'observation_only'}
            return {'state': row['state'], 'authority': 'observation_only', 'source_id': source_id,
                    'project': project, 'source_digest': row['digest'], 'source_binding': json.loads(row['binding'])}

    def source_evidence(self, source_id, project, digest, expected_binding):
        """Read one source observation; absence never proves a POST had no effect.

        A present observation does not identify a worker incarnation or grant
        permission to release a hold. No journal, files, or bindings are changed.
        """
        ensure(_SOURCE_EVIDENCE_ENABLED, 'source_evidence_unavailable', 503)
        identifier(source_id)
        ensure(type(project) is str and project in self.projects, 'project_not_allowed', 403)
        ensure(type(digest) is str and re.fullmatch(r'[0-9a-f]{64}', digest), 'invalid_source_digest')
        ensure(type(expected_binding) is dict and set(expected_binding) == {'scheme', 'commit_oid', 'tree_oid',
               'archive_sha256', 'commit_object_sha256', 'expanded_bytes', 'entries'}
               and expected_binding.get('scheme') == 'git-sha1-tree-v1'
               and all(type(expected_binding.get(key)) is str and re.fullmatch(r'[0-9a-f]{40}', expected_binding[key])
                       for key in ('commit_oid', 'tree_oid'))
               and all(type(expected_binding.get(key)) is str and re.fullmatch(r'[0-9a-f]{64}', expected_binding[key])
                       for key in ('archive_sha256', 'commit_object_sha256'))
               and expected_binding['archive_sha256'] == digest
               and type(expected_binding['expanded_bytes']) is int and 0 <= expected_binding['expanded_bytes'] <= MAX_EXPANDED
               and type(expected_binding['entries']) is int and 1 <= expected_binding['entries'] <= 5000,
               'invalid_source_binding')
        unknown = {'state': 'unknown', 'authority': 'observation_only'}
        with self.lock:
            try:
                row = self.db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
                if row is None:
                    return unknown
                if row['project'] != project or row['digest'] != digest:
                    return {'state': 'conflict', 'authority': 'observation_only'}
                binding_row = self.db.execute('SELECT * FROM source_bindings WHERE source_id=?', (source_id,)).fetchone()
                if (binding_row is None or binding_row['source_inode'] != row['inode']
                        or binding_row['source_created'] != row['created']
                        or type(row['bytes']) is not int or not 0 < row['bytes'] <= MAX_ARCHIVE):
                    return unknown
                def unique(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError('duplicate binding key')
                        result[key] = value
                    return result
                binding = json.loads(binding_row['binding'], object_pairs_hook=unique)
                if (type(binding) is not dict or set(binding) != {'scheme', 'commit_oid', 'tree_oid',
                        'archive_sha256', 'commit_object_sha256', 'expanded_bytes', 'entries'}
                        or binding.get('scheme') != 'git-sha1-tree-v1'
                        or any(type(binding.get(key)) is not str or re.fullmatch(r'[0-9a-f]{40}', binding[key]) is None
                               for key in ('commit_oid', 'tree_oid'))
                        or any(type(binding.get(key)) is not str or re.fullmatch(r'[0-9a-f]{64}', binding[key]) is None
                               for key in ('archive_sha256', 'commit_object_sha256'))
                        or binding['archive_sha256'] != digest
                        or type(binding['expanded_bytes']) is not int or not 0 <= binding['expanded_bytes'] <= MAX_EXPANDED
                        or type(binding['entries']) is not int or not 1 <= binding['entries'] <= 5000):
                    return unknown
                if binding != expected_binding:
                    return {'state': 'conflict', 'authority': 'observation_only'}
                if not all(hasattr(os, name) for name in ('O_NOFOLLOW', 'O_DIRECTORY', 'O_NONBLOCK')):
                    return unknown
                directory = os.open(self.sources, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    fd = os.open(source_id + '.zip', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                    try:
                        before = os.fstat(fd)
                        if not stat.S_ISREG(before.st_mode) or before.st_ino != row['inode'] or before.st_size != row['bytes']:
                            return unknown
                        with os.fdopen(fd, 'rb', closefd=False) as stream:
                            raw = stream.read(MAX_ARCHIVE + 1)
                        after = os.fstat(fd)
                        linked = os.stat(source_id + '.zip', dir_fd=directory, follow_symlinks=False)
                        if (len(raw) != row['bytes'] or hashlib.sha256(raw).hexdigest() != digest
                                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                                or (linked.st_dev, linked.st_ino) != (after.st_dev, after.st_ino)):
                            return unknown
                    finally:
                        os.close(fd)
                finally:
                    os.close(directory)
                from .source_provenance import verify_tree_binding
                verify_tree_binding(raw, binding)
                return {'state': 'present', 'authority': 'observation_only',
                        'binding_assurance': 'journal_match_tree_checked', 'source_id': source_id,
                        'project': project, 'source_digest': digest, 'size_bytes': len(raw),
                        'source_binding': binding}
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                return unknown

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

    def _source_binding(self, source_id, project, digest):
        row = self.db.execute('SELECT * FROM sources WHERE id=?', (identifier(source_id),)).fetchone()
        ensure(row is not None and row['project'] == project and row['digest'] == digest,
               'source_not_registered', 404)
        binding_row = self.db.execute('SELECT source_inode,source_created,binding FROM source_bindings WHERE source_id=?',
                                      (source_id,)).fetchone()
        if binding_row is None:
            return None
        if (binding_row['source_inode'] != row['inode']
                or binding_row['source_created'] != row['created']):
            # The older worker can delete and recreate a source with the same
            # UUID without knowing this side table. The file-incarnation facts
            # prevent attaching the previous proof to that new unverified row.
            self.db.execute('DELETE FROM source_bindings WHERE source_id=?', (source_id,))
            self.db.commit()
            return None
        try:
            value = json.loads(binding_row['binding'])
        except (ValueError, TypeError):
            raise Rejected('source_binding_corrupt', 409) from None
        ensure(type(value) is dict, 'source_binding_corrupt', 409)
        return value

    def _source_binding_readonly(self, source_id, project, digest):
        """Read a binding without deleting or committing stale state."""
        row = self.db.execute('SELECT * FROM sources WHERE id=?', (identifier(source_id),)).fetchone()
        ensure(row is not None and row['project'] == project and row['digest'] == digest,
               'source_not_registered', 404)
        bound = self.db.execute('SELECT source_inode,source_created,binding FROM source_bindings WHERE source_id=?',
                                (source_id,)).fetchone()
        if bound is None:
            return None
        ensure(bound['source_inode'] == row['inode'] and bound['source_created'] == row['created'],
               'source_binding_corrupt', 409)
        try:
            value = json.loads(bound['binding'])
        except (ValueError, TypeError):
            raise Rejected('source_binding_corrupt', 409) from None
        ensure(type(value) is dict, 'source_binding_corrupt', 409)
        return value

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
                self.db.execute('DELETE FROM source_bindings WHERE source_id=?', (row['id'],))
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

    def _legacy_submit(self, payload):
        fields = {'job_id', 'command_id', 'idempotency_key', 'project', 'recipe', 'source_commit', 'source_digest', 'resource_ids'}
        legacy = (fields | {'source_id'}, fields | {'source_zip_base64'})
        bound = fields | {'source_id', 'require_source_binding', 'expected_source_binding'}
        ensure(isinstance(payload, dict) and set(payload) in (*legacy, bound), 'job_schema')
        if 'require_source_binding' in payload:
            ensure(payload['require_source_binding'] is True and type(payload['expected_source_binding']) is dict,
                   'job_schema')
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
            attempt_schema = False
            if ('source_id' in payload and self.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='source_attempts'").fetchone() is not None):
                self.db.execute('BEGIN IMMEDIATE')
                try:
                    attempt_schema = self._guard_source_attempt(payload['source_id'], payload['project'], payload['source_digest'], None, None)
                    self.db.commit()
                except Exception:
                    self.db.rollback()
                    raise
            raw = self._source(payload['source_id'], payload['project'], payload['source_digest']) if 'source_id' in payload else archive(payload['source_zip_base64'])
            ensure(hashlib.sha256(raw).hexdigest() == payload['source_digest'], 'source_digest_mismatch')
            binding_reader = self._source_binding_readonly if attempt_schema else self._source_binding
            binding = (binding_reader(payload['source_id'], payload['project'], payload['source_digest'])
                       if 'source_id' in payload else None)
            if payload.get('require_source_binding') is True:
                ensure(binding is not None and
                       json.dumps(binding, sort_keys=True, separators=(',', ':')) ==
                       json.dumps(payload['expected_source_binding'], sort_keys=True, separators=(',', ':')),
                       'source_binding_required', 409)
            if binding is not None:
                from .source_provenance import verify_tree_binding
                try:
                    verify_tree_binding(raw, binding)
                except ValueError:
                    raise Rejected('source_binding_invalid', 409) from None
                ensure(binding['commit_oid'] == payload['source_commit'], 'source_binding_mismatch', 409)
                payload = dict(payload, source_binding=binding)
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
            self.db.execute('BEGIN IMMEDIATE')
            try:
                if 'source_id' in payload:
                    self._guard_source_attempt(payload['source_id'], payload['project'], payload['source_digest'], None, None)
            except Exception:
                self.db.rollback()
                raise
            try:
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


    def submit(self, payload):
        if isinstance(payload, dict) and 'reviewed_profile' in payload:
            return self._submit_v2(payload)
        return self._legacy_submit(payload)

    def _submit_v2(self, payload):
        ensure(_PROFILE_PROTOCOL_READY, 'profile_protocol_unavailable', 503)
        fields = {'job_id', 'command_id', 'idempotency_key', 'project', 'recipe', 'source_commit', 'source_digest', 'resource_ids'}
        bound = fields | {'source_id', 'require_source_binding', 'expected_source_binding'}
        reviewed = bound | {'reviewed_profile'}
        # Old accepted operations remain queryable/held. No new name-only or
        # source-only operation may enter the worker after this protocol change.
        ensure(type(payload) is dict and set(payload) == reviewed, 'job_schema')
        try:
            payload = json.loads(json.dumps(payload, allow_nan=False))
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise Rejected('job_schema') from None
        if 'require_source_binding' in payload:
            ensure(payload['require_source_binding'] is True and type(payload['expected_source_binding']) is dict,
                   'job_schema')
        identifier(payload['job_id']); identifier(payload['command_id'])
        ensure(isinstance(payload['idempotency_key'], str) and 0 < len(payload['idempotency_key']) <= 128,
               'invalid_idempotency_key')
        ensure(payload['project'] in self.projects, 'project_not_allowed', 403)
        ensure(payload['recipe'] in self.projects[payload['project']], 'recipe_not_allowed', 403)
        profile = self.projects[payload['project']][payload['recipe']]
        from .profile_binding import checked_recipe_snapshot
        try:
            profile = checked_recipe_snapshot(payload['project'], payload['recipe'],
                                              payload['reviewed_profile'], profile)
        except ValueError:
            raise Rejected('reviewed_profile_mismatch', 409) from None
        ensure(re.fullmatch(r'[0-9a-f]{40}', payload.get('source_commit', '')) is not None, 'invalid_commit')
        ensure(re.fullmatch(r'[0-9a-f]{64}', payload.get('source_digest', '')) is not None, 'invalid_source_digest')
        ensure(isinstance(payload['resource_ids'], dict) and set(payload['resource_ids']) == set(KINDS), 'resource_schema')
        ids = [identifier(payload['resource_ids'][kind]) for kind in KINDS]
        ensure(len(set(ids)) == len(ids), 'duplicate_resource_id')
        with self.lock:
            ensure(_PROFILE_PROTOCOL_READY, 'profile_protocol_unavailable', 503)
            identity, existing = self._operation(payload['command_id'], payload)
            if existing:
                return existing
            raw = self._source(payload['source_id'], payload['project'], payload['source_digest']) if 'source_id' in payload else archive(payload['source_zip_base64'])
            ensure(hashlib.sha256(raw).hexdigest() == payload['source_digest'], 'source_digest_mismatch')
            binding = (self._source_binding_readonly(payload['source_id'], payload['project'], payload['source_digest'])
                       if 'source_id' in payload else None)
            if payload.get('require_source_binding') is True:
                ensure(binding is not None and
                       json.dumps(binding, sort_keys=True, separators=(',', ':')) ==
                       json.dumps(payload['expected_source_binding'], sort_keys=True, separators=(',', ':')),
                       'source_binding_required', 409)
            if binding is not None:
                from .source_provenance import verify_tree_binding
                try:
                    verify_tree_binding(raw, binding)
                except ValueError:
                    raise Rejected('source_binding_invalid', 409) from None
                ensure(binding['commit_oid'] == payload['source_commit'], 'source_binding_mismatch', 409)
                payload = dict(payload, source_binding=binding)
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
            self.db.execute('BEGIN IMMEDIATE')
            try:
                ensure(_SOURCE_ATTEMPT_ENABLED, 'source_attempt_unavailable', 503)
                self._require_observed_source_attempt(payload)
            except Exception:
                self.db.rollback()
                raise
            try:
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
            self.thread = threading.Thread(target=self._execute, args=(payload, raw, profile), daemon=False)
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

    def _execute(self, payload, raw, profile=None):
        try:
            self._execute_operation(payload, raw, profile)
        except Exception:
            # Preserve the original reservation even if SQLite cannot record
            # failure. The sticky veto and startup accepted/running->held gate
            # prevent another job; private receipt remains evidence only.
            with self.lock:
                if self._receipt_config is not None:
                    self._receipt_fault = 'terminal_receipt_persistence_failed'
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

    def _execute_operation(self, payload, raw, profile=None):
        lease = None
        try:
            executable_paths = frozenset()
            if payload.get('source_binding') is not None:
                from .source_provenance import verified_executable_paths
                executable_paths = verified_executable_paths(raw, payload['source_binding'])
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
            make_job_readable(paths['workspace'], MAX_EXPANDED, executable_paths=executable_paths)
            if profile is None:
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
            result.update(source_commit=payload['source_commit'], source_commit_verification=(
                              'git-sha1-tree-v1' if payload.get('source_binding') else 'declared'),
                          source_digest=payload['source_digest'], resource_ids=payload['resource_ids'])
            if payload.get('source_binding') is not None:
                result['source_binding'] = payload['source_binding']
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
                with self.lock:
                    self._receipt_fault = 'terminal_receipt_persistence_failed'
        if self.scratch_slot is not None and (lease is None or not lease.backend_absent):
            state = 'held'
            result = {'error': 'scratch_cleanup_unproven'}
        if state in TERMINAL and 'reviewed_profile' in payload:
            result['profile_digest'] = payload['reviewed_profile']['digest']
        if state in TERMINAL and payload.get('source_binding') is not None:
            result.update(source_commit=payload['source_commit'], source_digest=payload['source_digest'],
                          source_commit_verification='git-sha1-tree-v1', source_binding=payload['source_binding'])
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

    def _cleanup_parent(self, expected):
        info = self.resources.stat(follow_symlinks=False)
        ensure(stat.S_ISDIR(info.st_mode) and [info.st_dev, info.st_ino] == expected
               and not self.resources.is_symlink(), 'cleanup_scope_drift', 409)
        if os.name != 'posix':
            ensure(self._receipt_config is None, 'terminal_receipts_posix_required')
            return None  # Legacy synthetic Windows service only; no receipt proof.
        fd = os.open(self.resources, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            actual = os.fstat(fd)
            ensure([actual.st_dev, actual.st_ino] == expected and actual.st_uid == os.getuid()
                   and not stat.S_IMODE(actual.st_mode) & 0o077, 'cleanup_scope_drift', 409)
            return fd
        except BaseException:
            os.close(fd); raise

    def _delete_cleanup_resource(self, item, root_identity):
        """Scoped ENOENT+parent fsync observation, not universal crash proof."""
        parent = self._cleanup_parent(root_identity)
        try:
            path = self.resources / item['id']
            actual = (os.stat(item['id'], dir_fd=parent, follow_symlinks=False) if parent is not None
                      else path.stat(follow_symlinks=False))
            ensure(stat.S_ISDIR(actual.st_mode) and actual.st_ino == item['inode']
                   and actual.st_dev == root_identity[0], 'cleanup_scope_drift', 409)
            manifest, _ = tree_manifest(path, MAX_EXPANDED)
            ensure(hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',', ':')).encode()).hexdigest() ==
                   item['_observed_manifest_sha256'], 'cleanup_scope_drift', 409)
            if parent is None:
                shutil.rmtree(path)
                ensure(not path.exists() and not path.is_symlink(), 'cleanup_uncertain', 409)
            else:
                shutil.rmtree(item['id'], dir_fd=parent)
                def absent():
                    try: os.stat(item['id'], dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError: return
                    raise Rejected('cleanup_uncertain', 409)
                absent()
                os.fsync(parent)
                absent()
                check = self._cleanup_parent(root_identity)
                os.close(check)
        finally:
            if parent is not None: os.close(parent)

    def cleanup(self, payload):
        ensure(isinstance(payload, dict) and set(payload) == {'command_id', 'idempotency_key', 'resources'}, 'cleanup_schema')
        identifier(payload['command_id'])
        ensure(isinstance(payload['idempotency_key'], str) and 0 < len(payload['idempotency_key']) <= 128,
               'invalid_idempotency_key')
        ensure(isinstance(payload['resources'], list) and 0 < len(payload['resources']) <= 4, 'cleanup_resources')
        with self.lock:
            identity, existing = self._operation(payload['command_id'], payload)
            if existing:
                return existing  # Original-ID read, never a repeated delete.
            ensure(self._receipt_fault is None, 'terminal_receipt_recovery_required', 409)
            ensure(self.active is None, 'worker_busy', 409)
            self._receipt_inventory()
            ensure(self._receipt_fault is None, 'terminal_receipt_recovery_required', 409)
            root = self.resources.stat(follow_symlinks=False)
            root_identity = [root.st_dev, root.st_ino]
            items = []
            for wanted in payload['resources']:
                ensure(isinstance(wanted, dict) and set(wanted) == {'resource_id', 'expected_revision'}, 'cleanup_resource_schema')
                item = self.metadata(wanted['resource_id'])
                ensure(item['state'] != 'deleted' and type(wanted['expected_revision']) is int and
                       wanted['expected_revision'] == item['revision'] and not item['refs'], 'cleanup_revision_or_reference', 409)
                ensure(self.status(item['job_id'])['state'] in TERMINAL, 'resource_job_not_terminal', 409)
                path = self.resources / item['id']
                ensure(path.parent == self.resources and not path.is_symlink() and path.is_dir() and
                       path.stat().st_ino == item['inode'] and path.stat().st_dev == root.st_dev, 'cleanup_scope_drift', 409)
                manifest, _ = tree_manifest(path, MAX_EXPANDED)
                item['_observed_manifest_sha256'] = hashlib.sha256(
                    json.dumps(manifest,sort_keys=True,separators=(',', ':')).encode()).hexdigest()
                items.append(item)
            ensure(len({x['id'] for x in items}) == len(items), 'duplicate_cleanup_resource')
            confirmed, current, terminal_attempted = [], None, False
            try:
                self.db.execute('BEGIN IMMEDIATE')
                binding = self._cleanup_binding(payload, identity, items, root_identity)
                self._prepare_receipt(payload, identity, binding=binding)
                self.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',
                                (payload['command_id'], identity, None, 'running', json.dumps(payload), '{}'))
                self.db.commit()  # Before the first deletion effect.
                for item in items:
                    current = item
                    self.db.execute("UPDATE resources SET state='deleting' WHERE id=?", (item['id'],))
                    self.db.commit()
                    self._delete_cleanup_resource(item, root_identity)
                    confirmed.append(item['id'])
                    self.db.execute("UPDATE resources SET state='deleted',revision=revision+1,manifest='{}',bytes=0 WHERE id=?", (item['id'],))
                    self.db.commit()
                result = {'deleted_resource_ids': [x['id'] for x in items]}
                terminal_attempted = True
                self._terminal_receipt(payload['command_id'], 'succeeded', result)
                self.db.execute("UPDATE operations SET state='succeeded',result=? WHERE command_id=?",
                                (json.dumps(result), payload['command_id']))
                self.db.commit()
                return {'command_id': payload['command_id'], 'job_id': None, 'state': 'succeeded', 'result': result}
            except Exception:
                # Latch FIRST: both a fallback hold write and its commit may fail.
                self._receipt_fault = 'cleanup_recovery_required'
                result = {'error': 'cleanup_uncertain', 'deleted_resource_ids': confirmed}
                try: self.db.rollback()
                except Exception: pass
                if not terminal_attempted:
                    try: self._terminal_receipt(payload['command_id'], 'held', result)
                    except Exception: pass  # Original slot remains uncertain/owned.
                try:
                    if current is not None:
                        self.db.execute("UPDATE resources SET state='delete_failed' WHERE id=? AND state!='deleted'", (current['id'],))
                    self.db.execute("UPDATE operations SET state='held',result=? WHERE command_id=?",
                                    (json.dumps(result), payload['command_id']))
                    self.db.commit()
                except Exception:
                    try: self.db.rollback()
                    except Exception: pass
                raise Rejected('cleanup_uncertain', 409) from None

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
