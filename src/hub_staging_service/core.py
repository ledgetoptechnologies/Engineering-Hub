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

# Independent development interlock for direct worker POSTs. The scoped
# prototype intentionally rejects old v2 HTTP recovery reads; the replacement
# lifecycle is incomplete. Do not enable before protocol review.
_PROFILE_PROTOCOL_READY = False
PROFILE_PROTOCOL = 'hub-staging-profile.v2'

MAX_ARCHIVE = 8 * 1024 * 1024
# This observation is not an authenticated ledger epoch or authority to retry.
_SOURCE_EVIDENCE_ENABLED = False
# Experimental receiver record only; does not confer worker or epoch authority.
_SOURCE_ATTEMPT_ENABLED = False
_SOURCE_ATTEMPT_LIMIT = 4096
_SOURCE_ATTEMPT_INTERNAL = object()  # Same-process routing, not a security capability.
_SCOPED_SOURCE_ATTEMPT_INTERNAL = object()  # Same-process routing, not a security capability.
_SOURCE_ATTEMPT_DDL = ("CREATE TABLE source_attempts (source_id TEXT PRIMARY KEY, "
                       "project TEXT NOT NULL, digest TEXT NOT NULL, binding TEXT NOT NULL, "
                       "state TEXT NOT NULL, source_inode INTEGER, source_created REAL)")
_SOURCE_OWNER_DDL = ("CREATE TABLE source_attempt_owners (source_id TEXT PRIMARY KEY, "
                     "principal TEXT NOT NULL, project TEXT NOT NULL, recipe TEXT NOT NULL, profile TEXT NOT NULL, "
                     "worker_dev INTEGER NOT NULL, worker_ino INTEGER NOT NULL, parent_dev INTEGER NOT NULL, parent_ino INTEGER NOT NULL)")
_SOURCE_WITNESS_DDL = ("CREATE TABLE source_attempt_witnesses (source_id TEXT PRIMARY KEY, "
                       "principal TEXT NOT NULL, project TEXT NOT NULL, recipe TEXT NOT NULL, profile TEXT NOT NULL, "
                       "worker_dev INTEGER NOT NULL, worker_ino INTEGER NOT NULL, parent_dev INTEGER NOT NULL, parent_ino INTEGER NOT NULL)")
_SCOPED_JOB_DDL = ("CREATE TABLE scoped_jobs (command_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE, "
                   "principal TEXT NOT NULL, project TEXT NOT NULL, source_id TEXT NOT NULL, resources TEXT NOT NULL)")
_SCOPED_CLEANUP_DDL = ("CREATE TABLE scoped_cleanups (command_id TEXT PRIMARY KEY, principal TEXT NOT NULL, "
                       "project TEXT NOT NULL, job_id TEXT NOT NULL)")
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


def _scoped_profile_json(project, recipe, value):
    """Canonical tuple validation; this does not establish independent review."""
    ensure(type(value) is dict and set(value) == {'project', 'recipe', 'generation', 'digest'}
           and value['project'] == project and value['recipe'] == recipe
           and type(value['generation']) is int and 1 <= value['generation'] < 2**31
           and type(value['digest']) is str and re.fullmatch(r'sha256:[0-9a-f]{64}', value['digest']),
           'reviewed_profile_mismatch', 409)
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


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
        initial_root = self.root.stat(follow_symlinks=False)
        ensure(stat.S_ISDIR(initial_root.st_mode), 'worker_root_invalid')
        self._root_identity = initial_root.st_dev, initial_root.st_ino
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
            journal = (self.root / 'journal.sqlite3').stat(follow_symlinks=False)
            ensure(stat.S_ISREG(journal.st_mode), 'journal_identity_invalid', 503)
            self._status_journal_identity = (journal.st_dev, journal.st_ino)
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
        original_fields = {'command_id', 'idempotency_key', 'resources'}
        scoped_fields = original_fields | {'_receiver_principal', '_receiver_job'}
        ensure(binding.get('operation_kind') == 'cleanup' and operation['job_id'] is None
               and type(payload) is dict and set(payload) in (original_fields, scoped_fields)
               and payload['command_id'] == operation['command_id'] == binding['command_id']
               and binding['operation_identity'] == operation['identity']
               and hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',', ':')).encode()).hexdigest() == operation['identity']
               and type(payload['resources']) is list and len(payload['resources']) == len(binding['resources']),
               'terminal_receipt_binding_changed')
        scoped_record = self.db.execute("SELECT type FROM sqlite_master WHERE name='scoped_cleanups'").fetchone()
        if set(payload) == scoped_fields:
            self._scoped_schema()
            owner = self.db.execute('SELECT * FROM scoped_cleanups WHERE command_id=?', (operation['command_id'],)).fetchone()
            ensure(owner is not None and owner['principal'] == payload['_receiver_principal']
                   and owner['job_id'] == payload['_receiver_job'], 'terminal_receipt_binding_changed')
            job_row = self.db.execute('SELECT payload FROM operations WHERE job_id=?', (owner['job_id'],)).fetchone()
            ensure(job_row is not None, 'terminal_receipt_binding_changed')
            saved_job = self._receipt_json(job_row['payload'])
            ensure(type(saved_job) is dict and saved_job.get('project') == owner['project']
                   and type(saved_job.get('recipe')) is str, 'terminal_receipt_binding_changed')
            self._scoped_job(owner['job_id'], owner['principal'], {owner['project']},
                             {owner['project']: {saved_job['recipe']}})
            self._require_observed_source_attempt(saved_job, principal=owner['principal'])
            ensure(all(item['job_id'] == owner['job_id'] for item in binding['resources']),
                   'terminal_receipt_binding_changed')
        elif scoped_record is not None:
            self._scoped_schema()
            ensure(self.db.execute('SELECT 1 FROM scoped_cleanups WHERE command_id=?',
                                   (operation['command_id'],)).fetchone() is None, 'terminal_receipt_binding_changed')
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
                    saved_job = self._receipt_json(operation['payload'])
                    ensure(type(saved_job) is dict, 'terminal_receipt_binding_changed')
                    scoped_table = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='scoped_jobs'").fetchone()
                    scoped_row = None
                    if scoped_table is not None:
                        self._scoped_schema()
                        scoped_row = self.db.execute('SELECT * FROM scoped_jobs WHERE command_id=?',
                                                     (operation['command_id'],)).fetchone()
                    if '_receiver_principal' in saved_job or scoped_row is not None:
                        expected_fields = {'job_id', 'command_id', 'idempotency_key', 'project', 'recipe',
                            'source_commit', 'source_digest', 'resource_ids', 'source_id', 'require_source_binding',
                            'expected_source_binding', 'reviewed_profile', 'source_binding', '_receiver_principal'}
                        ensure(set(saved_job) == expected_fields and scoped_row is not None
                               and saved_job['_receiver_principal'] == scoped_row['principal']
                               and saved_job['project'] == scoped_row['project'] and type(saved_job['recipe']) is str,
                               'terminal_receipt_binding_changed')
                        original = {key: value for key, value in saved_job.items() if key != 'source_binding'}
                        ensure(hashlib.sha256(json.dumps(original,sort_keys=True,separators=(',', ':')).encode()).hexdigest()
                               == operation['identity'], 'terminal_receipt_binding_changed')
                        self._scoped_job(operation['job_id'], scoped_row['principal'], {scoped_row['project']},
                                         {scoped_row['project']: {saved_job['recipe']}})
                        self._require_observed_source_attempt(saved_job, principal=scoped_row['principal'])
                    elif 'reviewed_profile' in saved_job:
                        # A scoped job's identity includes its receiver principal.
                        # If its marker and table both disappear, it must not be
                        # mistaken for an earlier unscoped profile-bound job.
                        original = {key: value for key, value in saved_job.items() if key != 'source_binding'}
                        ensure(hashlib.sha256(json.dumps(original,sort_keys=True,separators=(',', ':')).encode()).hexdigest()
                               == operation['identity'], 'terminal_receipt_binding_changed')
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

    def _scoped_source_parent_identity(self, *, sync_worker_root=False):
        ensure(os.name == 'posix' and hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'),
               'scoped_source_posix_required', 503)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        worker_fd = os.open(self.root, flags)
        fd = None
        try:
            worker = os.fstat(worker_fd)
            worker_path = self.root.stat(follow_symlinks=False)
            ensure(stat.S_ISDIR(worker.st_mode) and worker.st_uid == os.getuid()
                   and not stat.S_IMODE(worker.st_mode) & 0o077
                   and (worker.st_dev, worker.st_ino) == (worker_path.st_dev, worker_path.st_ino) == self._root_identity,
                   'source_parent_identity_drift', 409)
            fd = os.open('sources', flags, dir_fd=worker_fd)
            root, current = os.fstat(fd), os.stat('sources', dir_fd=worker_fd, follow_symlinks=False)
            ensure(stat.S_ISDIR(root.st_mode) and root.st_uid == os.getuid()
                   and not stat.S_IMODE(root.st_mode) & 0o077
                   and stat.S_ISDIR(current.st_mode) and (root.st_dev, root.st_ino) == (current.st_dev, current.st_ino),
                   'source_parent_identity_drift', 409)
            if sync_worker_root:
                os.fsync(worker_fd)
                entry_after = os.stat('sources', dir_fd=worker_fd, follow_symlinks=False)
                worker_after = self.root.stat(follow_symlinks=False)
                ensure((entry_after.st_dev, entry_after.st_ino) == (root.st_dev, root.st_ino)
                       and (worker_after.st_dev, worker_after.st_ino) == (worker.st_dev, worker.st_ino),
                       'source_parent_identity_drift', 409)
            return worker.st_dev, worker.st_ino, root.st_dev, root.st_ino
        finally:
            if fd is not None:
                os.close(fd)
            os.close(worker_fd)

    def _scoped_parent_matches(self, owner):
        try:
            return self._scoped_source_parent_identity() == (owner['worker_dev'], owner['worker_ino'],
                                                             owner['parent_dev'], owner['parent_ino'])
        except (OSError, Rejected):
            return False

    def _scoped_publish_source(self, name, raw, expected_parent):
        """Publish and sync one scoped file; uncertain failures retain bytes.

        Successful fsync is local filesystem evidence, not an independent
        host/old-writer fence or universal power-loss guarantee.
        """
        ensure(os.name == 'posix' and hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'),
               'scoped_source_posix_required', 503)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        worker_fd = os.open(self.root, flags)
        parent = None
        child = None
        try:
            worker = os.fstat(worker_fd)
            worker_path = self.root.stat(follow_symlinks=False)
            ensure(stat.S_ISDIR(worker.st_mode) and worker.st_uid == os.getuid()
                   and not stat.S_IMODE(worker.st_mode) & 0o077
                   and (worker.st_dev, worker.st_ino) == (worker_path.st_dev, worker_path.st_ino) ==
                       self._root_identity == expected_parent[:2], 'source_parent_identity_drift', 409)
            parent = os.open('sources', flags, dir_fd=worker_fd)
            root = os.fstat(parent)
            path_root = os.stat('sources', dir_fd=worker_fd, follow_symlinks=False)
            ensure(stat.S_ISDIR(root.st_mode) and root.st_uid == os.getuid()
                   and not stat.S_IMODE(root.st_mode) & 0o077
                   and stat.S_ISDIR(path_root.st_mode)
                   and (root.st_dev, root.st_ino) == (path_root.st_dev, path_root.st_ino) == expected_parent[2:],
                   'source_parent_identity_drift', 409)
            child = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(child, 'wb') as stream:
                child = None
                os.fchmod(stream.fileno(), 0o600)
                initial = os.fstat(stream.fileno())
                ensure(stat.S_ISREG(initial.st_mode) and initial.st_nlink == 1 and initial.st_uid == os.getuid()
                       and stat.S_IMODE(initial.st_mode) == 0o600 and initial.st_dev == root.st_dev,
                       'source_file_identity_drift', 409)
                ensure(stream.write(raw) == len(raw), 'source_write_incomplete', 409)
                stream.flush()
                os.fsync(stream.fileno())
                before = os.fstat(stream.fileno())
                ensure(before.st_size == len(raw) and before.st_nlink == 1,
                       'source_file_identity_drift', 409)
                os.fsync(parent)
                entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
                after = os.fstat(stream.fileno())
                current_root = os.stat('sources', dir_fd=worker_fd, follow_symlinks=False)
                current_worker = self.root.stat(follow_symlinks=False)
                ensure(stat.S_ISREG(entry.st_mode) and entry.st_nlink == 1 and entry.st_uid == os.getuid()
                       and stat.S_IMODE(entry.st_mode) == 0o600
                       and (entry.st_dev, entry.st_ino, entry.st_size) == (before.st_dev, before.st_ino, before.st_size)
                       and (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) ==
                           (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                       and (current_root.st_dev, current_root.st_ino) == (root.st_dev, root.st_ino)
                       and (current_worker.st_dev, current_worker.st_ino) == (worker.st_dev, worker.st_ino),
                       'source_file_identity_drift', 409)
                return before.st_ino
        finally:
            if child is not None:
                os.close(child)
            if parent is not None:
                os.close(parent)
            os.close(worker_fd)

    def _scoped_verify_source(self, source_id, owner, source, digest, binding):
        """Read back exact bytes under pinned descriptors, without granting authority."""
        if not all(hasattr(os, name) for name in ('O_NOFOLLOW', 'O_DIRECTORY', 'O_NONBLOCK')):
            return False
        worker_fd = parent = child = None
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            worker_fd = os.open(self.root, flags)
            worker = os.fstat(worker_fd)
            worker_path = self.root.stat(follow_symlinks=False)
            if ((worker.st_dev, worker.st_ino) != (owner['worker_dev'], owner['worker_ino'])
                    or (worker.st_dev, worker.st_ino) != (worker_path.st_dev, worker_path.st_ino)
                    or worker.st_uid != os.getuid() or stat.S_IMODE(worker.st_mode) & 0o077):
                return False
            parent = os.open('sources', flags, dir_fd=worker_fd)
            directory = os.fstat(parent)
            linked_dir = os.stat('sources', dir_fd=worker_fd, follow_symlinks=False)
            if ((directory.st_dev, directory.st_ino) != (owner['parent_dev'], owner['parent_ino'])
                    or (directory.st_dev, directory.st_ino) != (linked_dir.st_dev, linked_dir.st_ino)
                    or directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) & 0o077):
                return False
            name = source_id + '.zip'
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            before = os.fstat(child)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_dev != directory.st_dev
                    or before.st_ino != source['inode'] or before.st_size != source['bytes']
                    or not 0 < before.st_size <= MAX_ARCHIVE):
                return False
            with os.fdopen(child, 'rb', closefd=False) as stream:
                raw = stream.read(MAX_ARCHIVE + 1)
            after = os.fstat(child)
            linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
            final_dir = os.stat('sources', dir_fd=worker_fd, follow_symlinks=False)
            final_worker = self.root.stat(follow_symlinks=False)
            if (len(raw) != source['bytes'] or hashlib.sha256(raw).hexdigest() != digest
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or (linked.st_dev, linked.st_ino) != (after.st_dev, after.st_ino)
                    or (final_dir.st_dev, final_dir.st_ino) != (directory.st_dev, directory.st_ino)
                    or (final_worker.st_dev, final_worker.st_ino) != (worker.st_dev, worker.st_ino)):
                return False
            from .source_provenance import verify_tree_binding
            verify_tree_binding(raw, binding)
            return True
        except (OSError, ValueError, TypeError, KeyError):
            return False
        finally:
            if child is not None:
                os.close(child)
            if parent is not None:
                os.close(parent)
            if worker_fd is not None:
                os.close(worker_fd)

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
                attempt_schema = self.db.execute("SELECT 1 FROM sqlite_master WHERE name IN "
                    "('source_attempts','source_attempt_owners','source_attempt_witnesses') LIMIT 1").fetchone()
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
                # An experimental scoped deposit must not acquire the legacy
                # worker-wide authority to collect other projects' sources.
                if _attempt_owner is not _SCOPED_SOURCE_ATTEMPT_INTERNAL:
                    self.cleanup_sources()
                self._capacity(len(raw))
                path = self.sources / (source_id + '.zip')
                ensure(not path.exists() and not path.is_symlink(), 'source_path_conflict', 409)
                created_inode = None
                self.db.execute('BEGIN IMMEDIATE')
                try:
                    self._guard_source_attempt(source_id, payload['project'], digest, binding_json, _attempt_owner)
                    if _attempt_owner in (_SOURCE_ATTEMPT_INTERNAL, _SCOPED_SOURCE_ATTEMPT_INTERNAL):
                        ensure(self.db.execute('SELECT 1 FROM source_bindings WHERE source_id=?', (source_id,)).fetchone() is None,
                               'source_attempt_conflict', 409)
                    # A previous worker does not enable SQLite foreign keys,
                    # so its exact legacy DELETE may leave this one orphan.
                    # Absence of the source row makes this binding stale.
                    self.db.execute('DELETE FROM source_bindings WHERE source_id=?', (source_id,))
                    if _attempt_owner is _SCOPED_SOURCE_ATTEMPT_INTERNAL:
                        owner_row = self.db.execute('SELECT worker_dev,worker_ino,parent_dev,parent_ino FROM source_attempt_owners WHERE source_id=?',
                                                    (source_id,)).fetchone()
                        ensure(owner_row is not None, 'source_owner_journal_invalid', 503)
                        created_inode = self._scoped_publish_source(path.name, raw,
                            (owner_row['worker_dev'], owner_row['worker_ino'], owner_row['parent_dev'], owner_row['parent_ino']))
                    else:
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
                    if (_attempt_owner is not _SCOPED_SOURCE_ATTEMPT_INTERNAL and created_inode is not None
                            and not path.is_symlink() and path.exists()
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
        owner_objects = self.db.execute("SELECT name FROM sqlite_master WHERE name IN "
            "('source_attempt_owners','source_attempt_witnesses')").fetchall()
        if owner_objects:
            ensure(len(objects) == 1 and objects[0]['type'] == 'table', 'source_owner_journal_invalid', 503)
            self._source_attempt_schema()
            self._source_owner_schema()
        if not objects:
            ensure(owner is None, 'source_attempt_journal_invalid', 503)
            return False
        ensure(len(objects) == 1 and objects[0]['type'] == 'table', 'source_attempt_journal_invalid', 503)
        self._source_attempt_schema()
        if not owner_objects:
            ensure(self.db.execute("SELECT 1 FROM source_attempts WHERE state IN "
                "('scoped_uncertain','scoped_historical_observation') LIMIT 1").fetchone() is None,
                'source_owner_journal_invalid', 503)
        row = self.db.execute('SELECT * FROM source_attempts WHERE source_id=?', (source_id,)).fetchone()
        if owner is None:
            ensure(row is None, 'source_attempt_reserved', 409)
        else:
            ensure(owner in (_SOURCE_ATTEMPT_INTERNAL, _SCOPED_SOURCE_ATTEMPT_INTERNAL) and row is not None and
                   (row['project'], row['digest'], row['binding']) == (project, digest, binding_json)
                   and row['state'] == ('scoped_uncertain' if owner is _SCOPED_SOURCE_ATTEMPT_INTERNAL else 'uncertain'),
                   'source_attempt_journal_invalid', 503)
        return True

    def _require_observed_source_attempt(self, payload, *, principal=None):
        """Admission check only; does not prove peer epoch or host fencing."""
        objects = self.db.execute("SELECT type FROM sqlite_master WHERE name='source_attempts'").fetchall()
        ensure(len(objects) == 1 and objects[0]['type'] == 'table', 'source_attempt_required', 409)
        self._source_attempt_schema()
        attempt = self.db.execute('SELECT * FROM source_attempts WHERE source_id=?', (payload['source_id'],)).fetchone()
        source = self.db.execute('SELECT * FROM sources WHERE id=?', (payload['source_id'],)).fetchone()
        binding = self.db.execute('SELECT * FROM source_bindings WHERE source_id=?', (payload['source_id'],)).fetchone()
        canonical = json.dumps(payload['expected_source_binding'], sort_keys=True, separators=(',', ':'))
        if principal is not None:
            self._source_owner_schema()
            owner = self.db.execute('SELECT * FROM source_attempt_owners WHERE source_id=?', (payload['source_id'],)).fetchone()
            profile_json = _scoped_profile_json(payload['project'], payload['recipe'], payload['reviewed_profile'])
            ensure(owner is not None and (owner['principal'], owner['project'], owner['recipe']) ==
                   (principal, payload['project'], payload['recipe']) and owner['profile'] == profile_json
                   and self._scoped_parent_matches(owner),
                   'scoped_not_found', 404)
        expected_state = 'scoped_historical_observation' if principal is not None else 'historical_observation'
        ensure(attempt is not None and source is not None and binding is not None
               and (attempt['project'], attempt['digest'], attempt['binding'], attempt['state']) ==
                   (payload['project'], payload['source_digest'], canonical, expected_state)
               and (source['project'], source['digest'], source['inode'], source['created']) ==
                   (payload['project'], payload['source_digest'], attempt['source_inode'], attempt['source_created'])
               and (binding['binding'], binding['source_inode'], binding['source_created']) ==
                   (canonical, source['inode'], source['created']), 'source_attempt_required', 409)

    def put_source_attempt(self, payload, *, principal=None, recipe=None, reviewed_profile=None):
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
        if principal is not None:
            ensure(type(recipe) is str and recipe in self.projects[payload['project']], 'scoped_not_found', 404)
            profile_json = _scoped_profile_json(payload['project'], recipe, reviewed_profile)
            from .profile_binding import checked_recipe_snapshot
            try:
                checked_recipe_snapshot(payload['project'], recipe, reviewed_profile, self.projects[payload['project']][recipe])
            except ValueError:
                raise Rejected('reviewed_profile_mismatch', 409) from None
        else:
            ensure(recipe is None and reviewed_profile is None, 'source_attempt_schema')
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
            parent_identity = (self._scoped_source_parent_identity(sync_worker_root=True)
                               if principal is not None else None)
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self._source_attempt_schema()
                if principal is not None:
                    try:
                        checked_recipe_snapshot(payload['project'], recipe, reviewed_profile,
                                                self.projects[payload['project']][recipe])
                    except (KeyError, ValueError):
                        raise Rejected('reviewed_profile_mismatch', 409) from None
                    self._source_owner_schema(create=True)
                previous = self.db.execute('SELECT * FROM source_attempts WHERE source_id=?', (source_id,)).fetchone()
                if previous is not None:
                    if principal is not None:
                        # Do not distinguish ownership or matching fingerprint
                        # in a response. Reuse still remains held, never replayed.
                        raise Rejected('source_attempt_unknown', 409)
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
                initial_state = 'scoped_uncertain' if principal is not None else 'uncertain'
                self.db.execute('INSERT INTO source_attempts VALUES(?,?,?,?,?,?,?)',
                                (source_id, payload['project'], digest, canonical, initial_state, None, None))
                if principal is not None:
                    self.db.execute('INSERT INTO source_attempt_owners VALUES(?,?,?,?,?,?,?,?,?)',
                                    (source_id, principal, payload['project'], recipe, profile_json, *parent_identity))
                    self.db.execute('INSERT INTO source_attempt_witnesses VALUES(?,?,?,?,?,?,?,?,?)',
                                    (source_id, principal, payload['project'], recipe, profile_json, *parent_identity))
                stored = self.db.execute('SELECT * FROM source_attempts WHERE source_id=?', (source_id,)).fetchone()
                ensure(stored is not None and tuple(stored) == (source_id, payload['project'], digest, canonical, initial_state, None, None),
                       'source_attempt_journal_invalid', 503)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            # Recheck even while holding the worker's process-local lock. A
            # separate DB writer is outside this prototype's trust boundary.
            ensure(self.db.execute('SELECT 1 FROM source_bindings WHERE source_id=?', (source_id,)).fetchone() is None,
                   'source_attempt_conflict', 409)
            receipt = self._legacy_put_source(payload, _attempt_owner=(
                _SCOPED_SOURCE_ATTEMPT_INTERNAL if principal is not None else _SOURCE_ATTEMPT_INTERNAL))
            # Confirming the local effect is not proof of process identity or
            # external quiescence. Failure here leaves the original uncertain.
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self._source_attempt_schema()
                if principal is not None:
                    self._source_owner_schema()
                    owner = self.db.execute('SELECT principal,project,recipe,profile,worker_dev,worker_ino,parent_dev,parent_ino FROM source_attempt_owners WHERE source_id=?',
                                            (source_id,)).fetchone()
                    ensure(owner is not None and (owner['principal'], owner['project'], owner['recipe'], owner['profile'],
                           owner['worker_dev'], owner['worker_ino'], owner['parent_dev'], owner['parent_ino']) ==
                           (principal, payload['project'], recipe, profile_json, *parent_identity)
                           and self._scoped_parent_matches(owner), 'source_owner_journal_invalid', 503)
                source = self.db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
                source_binding = self.db.execute('SELECT * FROM source_bindings WHERE source_id=?', (source_id,)).fetchone()
                ensure(source is not None and source_binding is not None and source['project'] == payload['project']
                       and source['digest'] == digest and source_binding['binding'] == canonical
                       and source_binding['source_inode'] == source['inode'] and source_binding['source_created'] == source['created'],
                       'source_attempt_conflict', 409)
                if principal is not None:
                    ensure(self._scoped_verify_source(source_id, owner, source, digest, binding),
                           'source_readback_failed', 409)
                final_state = 'scoped_historical_observation' if principal is not None else 'historical_observation'
                changed = self.db.execute("UPDATE source_attempts SET state=?,source_inode=?,source_created=? WHERE source_id=? "
                    "AND project=? AND digest=? AND binding=? AND state=?",
                    (final_state, source['inode'], source['created'], source_id, payload['project'], digest, canonical, initial_state)).rowcount
                ensure(changed == 1, 'source_attempt_journal_invalid', 503)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            result = {'authority': 'observation_only', 'state': 'historical_observation', **receipt}
            if principal is not None:
                result['reviewed_profile'] = json.loads(profile_json)
            return result

    def _source_owner_schema(self, *, create=False):
        names = (('source_attempt_owners', _SOURCE_OWNER_DDL), ('source_attempt_witnesses', _SOURCE_WITNESS_DDL))
        objects = [self.db.execute('SELECT type,sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
                   for name, _ in names]
        if all(row is None for row in objects) and create:
            for _, ddl in names:
                self.db.execute(ddl)
            objects = [self.db.execute('SELECT type,sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
                       for name, _ in names]
        ensure(all(row is not None and row['type'] == 'table' and row['sql'] == ddl
                   for row, (_, ddl) in zip(objects, names)), 'source_owner_journal_invalid', 503)
        for name, _ in names:
            extras = self.db.execute("SELECT type FROM sqlite_master WHERE tbl_name=? "
                                     "AND type IN ('trigger','index') AND name NOT LIKE 'sqlite_autoindex_%'", (name,)).fetchall()
            ensure(not extras, 'source_owner_journal_invalid', 503)
        mismatch = self.db.execute('SELECT 1 FROM source_attempt_owners o LEFT JOIN source_attempt_witnesses w '
            'ON o.source_id=w.source_id LEFT JOIN source_attempts a ON a.source_id=o.source_id '
            'WHERE w.source_id IS NULL OR o.principal!=w.principal OR o.project!=w.project OR o.recipe!=w.recipe OR o.profile!=w.profile '
            'OR o.worker_dev!=w.worker_dev OR o.worker_ino!=w.worker_ino OR o.parent_dev!=w.parent_dev OR o.parent_ino!=w.parent_ino '
            "OR a.source_id IS NULL OR a.project!=o.project OR a.state NOT IN ('scoped_uncertain','scoped_historical_observation') LIMIT 1").fetchone()
        reverse = self.db.execute('SELECT 1 FROM source_attempt_witnesses w LEFT JOIN source_attempt_owners o '
                                  'ON o.source_id=w.source_id WHERE o.source_id IS NULL LIMIT 1').fetchone()
        missing = self.db.execute("SELECT 1 FROM source_attempts a LEFT JOIN source_attempt_owners o ON a.source_id=o.source_id "
            "WHERE a.state IN ('scoped_uncertain','scoped_historical_observation') AND o.source_id IS NULL LIMIT 1").fetchone()
        ensure(mismatch is None and reverse is None and missing is None, 'source_owner_journal_invalid', 503)
        for owner in self.db.execute('SELECT project,recipe,profile,worker_dev,worker_ino,parent_dev,parent_ino FROM source_attempt_owners'):
            try:
                saved = json.loads(owner['profile'])
                canonical = _scoped_profile_json(owner['project'], owner['recipe'], saved)
            except (Rejected, ValueError, TypeError):
                raise Rejected('source_owner_journal_invalid', 503) from None
            ensure(canonical == owner['profile'] and type(owner['worker_dev']) is int and owner['worker_dev'] >= 0
                   and type(owner['worker_ino']) is int and owner['worker_ino'] > 0
                   and type(owner['parent_dev']) is int and owner['parent_dev'] >= 0
                   and type(owner['parent_ino']) is int and owner['parent_ino'] > 0, 'source_owner_journal_invalid', 503)

    def source_attempt(self, source_id, project, *, principal=None, recipe=None):
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
            if principal is not None:
                owner_tables = self.db.execute("SELECT name FROM sqlite_master WHERE name IN "
                    "('source_attempt_owners','source_attempt_witnesses')").fetchall()
                if not owner_tables:
                    return {'state': 'unknown', 'authority': 'observation_only'}
                self._source_owner_schema()
                owner = self.db.execute('SELECT * FROM source_attempt_owners WHERE source_id=?', (source_id,)).fetchone()
                if owner is None or (owner['principal'], owner['project'], owner['recipe']) != (principal, project, recipe) or not self._scoped_parent_matches(owner):
                    return {'state': 'unknown', 'authority': 'observation_only'}
            row = self.db.execute('SELECT * FROM source_attempts WHERE source_id=?', (source_id,)).fetchone()
            states = ('scoped_uncertain', 'scoped_historical_observation') if principal is not None else ('uncertain', 'historical_observation')
            if row is None or row['project'] != project or row['state'] not in states:
                return {'state': 'unknown', 'authority': 'observation_only'}
            state = row['state'].removeprefix('scoped_')
            result = {'state': state, 'authority': 'observation_only', 'source_id': source_id,
                      'project': project, 'source_digest': row['digest'], 'source_binding': json.loads(row['binding'])}
            if principal is not None:
                result['reviewed_profile'] = json.loads(owner['profile'])
            return result

    def scoped_source_evidence(self, source_id, project, recipe, digest, expected_binding, principal, projects):
        """Scoped observation only; never proof of original receiver or quiescence."""
        ensure(_SOURCE_EVIDENCE_ENABLED, 'source_evidence_unavailable', 503)
        ensure(project in projects, 'scoped_not_found', 404)
        self._validate_source_evidence_query(source_id, project, digest, expected_binding)
        unknown = {'state': 'unknown', 'authority': 'observation_only'}
        with self.lock:
            attempts = self.db.execute("SELECT type FROM sqlite_master WHERE name='source_attempts'").fetchone()
            owner_tables = self.db.execute("SELECT name FROM sqlite_master WHERE name IN "
                "('source_attempt_owners','source_attempt_witnesses')").fetchall()
            if attempts is None:
                ensure(not owner_tables, 'source_owner_journal_invalid', 503)
                return unknown
            ensure(attempts['type'] == 'table', 'source_attempt_journal_invalid', 503)
            self._source_attempt_schema()
            if not owner_tables:
                ensure(self.db.execute("SELECT 1 FROM source_attempts WHERE state IN "
                    "('scoped_uncertain','scoped_historical_observation') LIMIT 1").fetchone() is None,
                    'source_owner_journal_invalid', 503)
                return unknown
            self._source_owner_schema()
            owner = self.db.execute('SELECT * FROM source_attempt_owners WHERE source_id=?', (source_id,)).fetchone()
            if owner is None or (owner['principal'], owner['project'], owner['recipe']) != (principal, project, recipe) or not self._scoped_parent_matches(owner):
                return unknown
            attempt = self.db.execute('SELECT * FROM source_attempts WHERE source_id=?', (source_id,)).fetchone()
            canonical = json.dumps(expected_binding, sort_keys=True, separators=(',', ':'))
            if (attempt is None or attempt['state'] != 'scoped_historical_observation'
                    or attempt['digest'] != digest or attempt['binding'] != canonical):
                return unknown
            source = self.db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
            saved_binding = self.db.execute('SELECT * FROM source_bindings WHERE source_id=?', (source_id,)).fetchone()
            if (source is None or saved_binding is None or source['project'] != project or source['digest'] != digest
                    or (attempt['source_inode'], attempt['source_created']) != (source['inode'], source['created'])
                    or (saved_binding['source_inode'], saved_binding['source_created'], saved_binding['binding']) !=
                       (source['inode'], source['created'], canonical)):
                return unknown
            if not self._scoped_verify_source(source_id, owner, source, digest, expected_binding):
                return unknown
            return {'state': 'present', 'authority': 'observation_only',
                    'binding_assurance': 'journal_match_tree_checked', 'source_id': source_id,
                    'project': project, 'source_digest': digest, 'size_bytes': source['bytes'],
                    'source_binding': expected_binding, 'reviewed_profile': json.loads(owner['profile'])}

    def _validate_source_evidence_query(self, source_id, project, digest, expected_binding):
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

    def source_evidence(self, source_id, project, digest, expected_binding):
        """Read one source observation; absence never proves a POST had no effect.

        A present observation does not identify a worker incarnation or grant
        permission to release a hold. No journal, files, or bindings are changed.
        """
        ensure(_SOURCE_EVIDENCE_ENABLED, 'source_evidence_unavailable', 503)
        self._validate_source_evidence_query(source_id, project, digest, expected_binding)
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
            # Scoped ownership has no collection protocol yet. Keep its files
            # while preserving preexisting unscoped retention behavior.
            owner_tables = self.db.execute("SELECT name FROM sqlite_master WHERE name IN "
                "('source_attempt_owners','source_attempt_witnesses')").fetchall()
            reserved = set()
            if owner_tables:
                self._source_owner_schema()
                ensure(all(self._scoped_parent_matches(owner) for owner in
                           self.db.execute('SELECT worker_dev,worker_ino,parent_dev,parent_ino FROM source_attempt_owners')),
                       'source_parent_identity_drift', 409)
                reserved = {row['source_id'] for row in self.db.execute('SELECT source_id FROM source_attempt_owners')}
            elif self.db.execute("SELECT 1 FROM sqlite_master WHERE name='source_attempts'").fetchone() is not None:
                self._source_attempt_schema()
                ensure(self.db.execute("SELECT 1 FROM source_attempts WHERE state IN "
                       "('scoped_uncertain','scoped_historical_observation') LIMIT 1").fetchone() is None,
                       'source_owner_journal_invalid', 503)
            scoped_tables = self.db.execute("SELECT name FROM sqlite_master WHERE name IN "
                "('scoped_jobs','scoped_cleanups')").fetchall()
            if scoped_tables:
                self._scoped_schema()
                admissions = self.db.execute('SELECT * FROM scoped_jobs').fetchall()
                if admissions:
                    ensure(bool(owner_tables) and self.db.execute("SELECT 1 FROM sqlite_master WHERE name='source_attempts'").fetchone(),
                           'source_owner_journal_invalid', 503)
                    self._source_attempt_schema()
                    self._source_owner_schema()
                for admission in admissions:
                    owner = self.db.execute('SELECT * FROM source_attempt_owners WHERE source_id=?',
                                            (admission['source_id'],)).fetchone()
                    ensure(owner is not None and (owner['principal'], owner['project']) ==
                           (admission['principal'], admission['project']), 'source_owner_journal_invalid', 503)
                    op = self.db.execute('SELECT payload FROM operations WHERE command_id=?',
                                         (admission['command_id'],)).fetchone()
                    ensure(op is not None, 'scoped_journal_invalid', 503)
                    saved = json.loads(op['payload'])
                    ensure(type(saved) is dict and type(saved.get('recipe')) is str, 'scoped_journal_invalid', 503)
                    ensure(owner['recipe'] == saved['recipe'] and owner['profile'] ==
                           _scoped_profile_json(saved['project'], saved['recipe'], saved.get('reviewed_profile')),
                           'source_owner_journal_invalid', 503)
                    self._scoped_job(admission['job_id'], admission['principal'], {admission['project']},
                                     {admission['project']: {saved['recipe']}})
                    reserved.add(admission['source_id'])
            references = {}
            for operation in self.db.execute('SELECT * FROM operations'):
                payload = json.loads(operation['payload'])
                ensure(type(payload) is dict, 'scoped_journal_invalid', 503)
                if '_receiver_principal' in payload:
                    ensure(bool(scoped_tables), 'scoped_journal_invalid', 503)
                    if operation['job_id'] is not None:
                        admission = self.db.execute('SELECT * FROM scoped_jobs WHERE command_id=? AND job_id=?',
                            (operation['command_id'], operation['job_id'])).fetchone()
                        ensure(admission is not None and admission['principal'] == payload['_receiver_principal'],
                               'scoped_journal_invalid', 503)
                    else:
                        cleanup = self.db.execute('SELECT * FROM scoped_cleanups WHERE command_id=?',
                                                  (operation['command_id'],)).fetchone()
                        ensure(cleanup is not None and cleanup['principal'] == payload['_receiver_principal']
                               and cleanup['job_id'] == payload.get('_receiver_job'), 'scoped_journal_invalid', 503)
                source_id = payload.get('source_id')
                if source_id is not None:
                    references.setdefault(source_id, []).append((operation, payload))
            for row in self.db.execute('SELECT * FROM sources WHERE created<?', ((time.time() if now is None else now) - 86400,)).fetchall():
                if row['id'] in reserved:
                    continue
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

    def _operation(self, command_id, payload, *, identity_scope=None):
        identity_input = payload if identity_scope is None else {**payload, **identity_scope}
        identity = hashlib.sha256(json.dumps(identity_input, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        row = self.db.execute('SELECT * FROM operations WHERE command_id=?', (command_id,)).fetchone()
        if row:
            ensure(row['identity'] == identity, 'idempotency_conflict', 409)
            return identity, self._view(row)
        key = payload['idempotency_key']
        for old in self.db.execute('SELECT payload FROM operations'):
            ensure(json.loads(old[0])['idempotency_key'] != key, 'idempotency_conflict', 409)
        return identity, None

    def _scoped_schema(self, *, create=False):
        """Validate receiver-owned admission tables; not an old-writer fence."""
        schemas = (('scoped_jobs', _SCOPED_JOB_DDL), ('scoped_cleanups', _SCOPED_CLEANUP_DDL))
        rows = [self.db.execute('SELECT type,sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
                for name, _ in schemas]
        if all(row is None for row in rows) and create:
            for _, ddl in schemas:
                self.db.execute(ddl)
            rows = [self.db.execute('SELECT type,sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
                    for name, _ in schemas]
        for (name, ddl), row in zip(schemas, rows):
            ensure(row is not None and row['type'] == 'table' and row['sql'] == ddl,
                   'scoped_journal_invalid', 503)
            ensure(not self.db.execute("SELECT 1 FROM sqlite_master WHERE tbl_name=? "
                   "AND type IN ('trigger','index') AND name NOT LIKE 'sqlite_autoindex_%'", (name,)).fetchone(),
                   'scoped_journal_invalid', 503)

    def _scoped_job(self, job_id, principal, projects, recipes):
        """Uniformly deny foreign, legacy, missing, and inconsistent jobs."""
        identifier(job_id)
        self._scoped_schema()
        item = self.db.execute('SELECT * FROM scoped_jobs WHERE job_id=?', (job_id,)).fetchone()
        ensure(item is not None and item['principal'] == principal and item['project'] in projects,
               'scoped_not_found', 404)
        operation = self.db.execute('SELECT * FROM operations WHERE command_id=? AND job_id=?',
                                    (item['command_id'], item['job_id'])).fetchone()
        ensure(operation is not None, 'scoped_journal_invalid', 503)
        try:
            stored = json.loads(operation['payload'])
            resources = json.loads(item['resources'])
        except (ValueError, TypeError):
            raise Rejected('scoped_journal_invalid', 503) from None
        ensure(type(stored) is dict and type(resources) is dict and set(resources) == set(KINDS)
               and stored.get('_receiver_principal') == principal and stored.get('project') == item['project']
               and stored.get('source_id') == item['source_id'] and stored.get('resource_ids') == resources,
               'scoped_journal_invalid', 503)
        ensure(stored.get('recipe') in recipes.get(item['project'], ()), 'scoped_not_found', 404)
        return item, resources

    def _scoped_resource(self, resource_id, principal, projects, recipes):
        identifier(resource_id)
        row = self.db.execute('SELECT * FROM resources WHERE id=?', (resource_id,)).fetchone()
        ensure(row is not None, 'scoped_not_found', 404)
        item, resources = self._scoped_job(row['job_id'], principal, projects, recipes)
        ensure(row['kind'] in KINDS and resources.get(row['kind']) == resource_id, 'scoped_journal_invalid', 503)
        return item

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
            attempt_schema = False
            if ('source_id' in payload and self.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name IN "
                    "('source_attempts','source_attempt_owners','source_attempt_witnesses') LIMIT 1").fetchone() is not None):
                self.db.execute('BEGIN IMMEDIATE')
                try:
                    attempt_schema = self._guard_source_attempt(payload['source_id'], payload['project'], payload['source_digest'], None, None)
                    self.db.commit()
                except Exception:
                    self.db.rollback()
                    raise
            identity, existing = self._operation(payload['command_id'], payload)
            if existing:
                return existing
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

    def _submit_v2(self, payload, *, principal=None, projects=None, recipes=None):
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
        if principal is not None:
            ensure(payload['project'] in projects and payload['recipe'] in recipes.get(payload['project'], ()),
                   'scoped_not_found', 404)
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
            if principal is not None:
                # Scope before looking up an idempotent operation or reading
                # bytes; caller JSON is not an authentication source.
                self._source_owner_schema()
                owner = self.db.execute('SELECT * FROM source_attempt_owners WHERE source_id=?', (payload['source_id'],)).fetchone()
                ensure(owner is not None and (owner['principal'], owner['project'], owner['recipe'], owner['profile']) ==
                       (principal, payload['project'], payload['recipe'],
                        _scoped_profile_json(payload['project'], payload['recipe'], payload['reviewed_profile']))
                       and self._scoped_parent_matches(owner),
                       'scoped_not_found', 404)
                old = self.db.execute('SELECT job_id FROM operations WHERE command_id=?', (payload['command_id'],)).fetchone()
                if old is not None:
                    ensure(old['job_id'] == payload['job_id'], 'scoped_not_found', 404)
                    item, _ = self._scoped_job(payload['job_id'], principal, projects, recipes)
                    ensure(item['command_id'] == payload['command_id'], 'scoped_not_found', 404)
            identity, existing = self._operation(payload['command_id'], payload,
                identity_scope={'_receiver_principal': principal} if principal is not None else None)
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
            if principal is not None:
                persisted['_receiver_principal'] = principal
            self.db.execute('BEGIN IMMEDIATE')
            try:
                ensure(_SOURCE_ATTEMPT_ENABLED, 'source_attempt_unavailable', 503)
                if principal is not None:
                    ensure(payload['project'] in projects and payload['recipe'] in recipes.get(payload['project'], ()),
                           'scoped_not_found', 404)
                self._require_observed_source_attempt(payload, principal=principal)
                if principal is not None:
                    self._scoped_schema(create=True)
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
                if principal is not None:
                    self.db.execute('INSERT INTO scoped_jobs VALUES (?,?,?,?,?,?)',
                        (payload['command_id'], payload['job_id'], principal, payload['project'], payload['source_id'],
                         json.dumps(payload['resource_ids'], sort_keys=True, separators=(',', ':'))))
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

    def _legacy_job_guard(self, job_id):
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name IN ('scoped_jobs','scoped_cleanups') LIMIT 1").fetchone():
            self._scoped_schema()
            ensure(self.db.execute('SELECT 1 FROM scoped_jobs WHERE job_id=?', (job_id,)).fetchone() is None,
                   'scoped_not_found', 404)
        row = self.db.execute('SELECT payload FROM operations WHERE job_id=?', (job_id,)).fetchone()
        ensure(row is not None, 'scoped_not_found', 404)
        saved = json.loads(row['payload'])
        ensure('_receiver_principal' not in saved, 'scoped_not_found', 404)

    def _legacy_resource_guard(self, resource_id):
        row = self.db.execute('SELECT job_id FROM resources WHERE id=?', (resource_id,)).fetchone()
        if row is not None:
            self._legacy_job_guard(row['job_id'])

    def legacy_status(self, job_id):
        with self.lock:
            self._legacy_job_guard(job_id)
            return self.status(job_id)

    def legacy_operation(self, command_id):
        with self.lock:
            row = self.db.execute('SELECT payload FROM operations WHERE command_id=?', (command_id,)).fetchone()
            if row is not None:
                ensure('_receiver_principal' not in json.loads(row['payload']), 'scoped_not_found', 404)
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name IN ('scoped_jobs','scoped_cleanups') LIMIT 1").fetchone():
                self._scoped_schema()
                ensure(self.db.execute('SELECT 1 FROM scoped_cleanups WHERE command_id=?', (command_id,)).fetchone() is None,
                       'scoped_not_found', 404)
            return self.operation(command_id)

    def legacy_metadata(self, resource_id):
        with self.lock:
            self._legacy_resource_guard(resource_id)
            return self.metadata(resource_id)

    def legacy_file(self, resource_id, name):
        with self.lock:
            self._legacy_resource_guard(resource_id)
            return self.file(resource_id, name)

    def legacy_diagnostic(self, resource_id, digest, name):
        with self.lock:
            self._legacy_resource_guard(resource_id)
            return self.diagnostic_file(resource_id, digest, name)

    def _status_without_active(self, *, db=None):
        """Require explicitly provisioned indexes; never scan/migrate at startup.

        Absence of unsettled local records is not proof of host quiescence.
        Operators must provision exact indexes during an independently reviewed,
        offline migration after measuring journal size and ensuring ownership.
        """
        try:
            ensure(self._receipt_fault is None, 'worker_status_unavailable', 503)
            operation_index = "CREATE INDEX status_unsettled_operations ON operations(state)\n                WHERE state NOT IN ('succeeded','failed') OR state IS NULL"
            lease_index = "CREATE INDEX status_unreleased_leases ON scratch_leases(state)\n                WHERE state != 'released' OR state IS NULL"
            db = self.db if db is None else db
            row = db.execute("""SELECT CASE WHEN
                (SELECT COUNT(*) FROM pragma_table_info('operations') WHERE name='state' AND "notnull"=1)=1 AND
                (SELECT COUNT(*) FROM pragma_table_info('scratch_leases') WHERE name='state' AND "notnull"=1)=1 AND
                EXISTS(SELECT 1 FROM sqlite_master WHERE type='index' AND name='status_unsettled_operations'
                       AND tbl_name='operations' AND sql=?) AND
                EXISTS(SELECT 1 FROM sqlite_master WHERE type='index' AND name='status_unreleased_leases'
                       AND tbl_name='scratch_leases' AND sql=?) THEN
                EXISTS(SELECT 1 FROM operations INDEXED BY status_unsettled_operations
                       WHERE state NOT IN ('succeeded','failed')) OR
                EXISTS(SELECT 1 FROM scratch_leases INDEXED BY status_unreleased_leases
                       WHERE state != 'released')
                ELSE 1 END""", (operation_index, lease_index)).fetchone()
            ensure(not row[0], 'worker_status_unavailable', 503)
        except (Rejected, sqlite3.Error):
            raise Rejected('worker_status_unavailable', 503) from None
        return None

    def _legacy_history_snapshot(self, *, inventory=False):
        """Bounded isolated snapshot; never infer a scope boundary from failure.

        Read-only status does not prove old writers, other machines or restored
        databases are fenced. Legacy-only large journals may need an explicitly
        reviewed index migration before status can be reported as idle.
        """
        probe = None
        root_path = self.root
        journal_path = root_path / 'journal.sqlite3'
        def check_identity():
            root = root_path.stat(follow_symlinks=False)
            journal = journal_path.stat(follow_symlinks=False)
            ensure(stat.S_ISDIR(root.st_mode) and (root.st_dev, root.st_ino) == self._root_identity
                   and stat.S_ISREG(journal.st_mode) and
                   (journal.st_dev, journal.st_ino) == self._status_journal_identity,
                   'worker_status_unavailable', 503)
        try:
            ensure(self._receipt_fault is None, 'worker_status_unavailable', 503)
            check_identity()
            probe = sqlite3.connect(journal_path.absolute().as_uri() + '?mode=ro', uri=True, timeout=0.05)
            probe.execute('PRAGMA query_only=ON')
            deadline = time.monotonic() + 0.25
            budget = [100]
            def limited():
                budget[0] -= 1
                return int(budget[0] <= 0 or time.monotonic() >= deadline)
            probe.set_progress_handler(limited, 1000)
            # The first read fixes one snapshot for scope, marker, and idle
            # checks; no external policy or network call occurs in this txn.
            probe.execute('BEGIN')
            scoped = probe.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name IN "
                "('source_attempts','source_attempt_owners','source_attempt_witnesses','scoped_jobs','scoped_cleanups') LIMIT 1").fetchone()
            if scoped is None:
                state_check = "" if inventory else "WHEN state IS NULL OR state NOT IN ('succeeded','failed') THEN 1"
                scoped = probe.execute("""SELECT 1 FROM operations WHERE
                    CASE WHEN typeof(payload) != 'text' OR length(payload) > 131072 OR NOT json_valid(payload) THEN 1
                         WHEN json_type(payload) != 'object' THEN 1
                         WHEN json_type(payload, '$._receiver_principal') IS NOT NULL THEN 1
                         """ + state_check + """ ELSE 0 END LIMIT 1""").fetchone()
            if scoped is None and not inventory:
                scoped = probe.execute("SELECT 1 FROM scratch_leases WHERE state IS NULL OR state != 'released' LIMIT 1").fetchone()
            result = (scoped is None if inventory else
                      self._status_without_active(db=probe) if scoped is not None else None)
            ensure(time.monotonic() < deadline, 'worker_status_unavailable', 503)
            check_identity()
            return result
        except (Rejected, OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
            raise Rejected('worker_status_unavailable', 503) from None
        finally:
            if probe is not None:
                cleanup_failed = False
                try:
                    probe.set_progress_handler(None, 0)
                except sqlite3.Error:
                    cleanup_failed = True
                try:
                    probe.close()
                except sqlite3.Error:
                    cleanup_failed = True
                if cleanup_failed:
                    raise Rejected('worker_status_unavailable', 503) from None

    def _legacy_idle_status(self):
        return self._legacy_history_snapshot()

    def legacy_active(self, *, scoped_mode=False):
        with self.lock:
            # Existing unscoped installs keep their original startup/status
            # behavior. Scoped configuration or retained scoped journal tables
            # require the indexed fail-closed status path, not a full scan.
            guarded = scoped_mode or self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name IN "
                "('source_attempts','source_attempt_owners','source_attempt_witnesses','scoped_jobs','scoped_cleanups') LIMIT 1").fetchone() is not None
            if guarded:
                ensure(self._receipt_fault is None, 'worker_status_unavailable', 503)
            if self.active is None:
                return self._status_without_active() if guarded else self._legacy_idle_status()
            row = self.db.execute('SELECT job_id,payload FROM operations WHERE command_id=?', (self.active,)).fetchone()
            if row is None or row['job_id'] is None:
                if guarded:
                    raise Rejected('worker_status_unavailable', 503)
                return None
            try:
                if guarded:
                    ensure(type(json.loads(row['payload'])) is dict, 'worker_status_unavailable', 503)
                self._legacy_job_guard(row['job_id'])
            except Rejected as exc:
                if exc.code == 'scoped_not_found':
                    if guarded:
                        raise Rejected('worker_status_unavailable', 503) from None
                    return None
                raise
            except (sqlite3.Error, ValueError, TypeError):
                if guarded:
                    raise Rejected('worker_status_unavailable', 503) from None
                raise
            return self.active

    def legacy_runtime_allowed(self):
        """Global runtime metadata is unavailable once scoped work is present."""
        with self.lock:
            # Never parse an unbounded history on the shared writer connection.
            # The short read-only snapshot also rejects malformed or scoped rows.
            return self._legacy_history_snapshot(inventory=True)

    def operation(self, command_id):
        """Read a previously issued cleanup outcome; never reapply its effect."""
        identifier(command_id)
        with self.lock:
            row = self.db.execute('SELECT * FROM operations WHERE command_id=?', (command_id,)).fetchone()
            ensure(row is not None, 'operation_not_found', 404)
            ensure(row['job_id'] is None, 'operation_not_cleanup', 409)
            return self._view(row)

    def scoped_status(self, job_id, principal, projects, recipes):
        with self.lock:
            self._scoped_job(job_id, principal, projects, recipes)
            return self.status(job_id)

    def scoped_active(self, principal, projects, recipes):
        with self.lock:
            ensure(self._receipt_fault is None, 'worker_status_unavailable', 503)
            if self.active is None:
                return self._status_without_active()
            try:
                row = self.db.execute('SELECT * FROM operations WHERE command_id=?', (self.active,)).fetchone()
                ensure(row is not None and row['job_id'] is not None, 'worker_status_unavailable', 503)
                saved = json.loads(row['payload'])
                ensure(type(saved) is dict and saved.get('_receiver_principal') == principal
                       and saved.get('project') in projects, 'worker_status_unavailable', 503)
                item, _ = self._scoped_job(row['job_id'], principal, projects, recipes)
                ensure(item['command_id'] == self.active, 'worker_status_unavailable', 503)
            except (Rejected, sqlite3.Error, ValueError, TypeError, KeyError):
                raise Rejected('worker_status_unavailable', 503) from None
            return item['command_id']

    def scoped_operation(self, command_id, principal, projects, recipes):
        identifier(command_id)
        with self.lock:
            self._scoped_schema()
            item = self.db.execute('SELECT * FROM scoped_cleanups WHERE command_id=?', (command_id,)).fetchone()
            ensure(item is not None and item['principal'] == principal and item['project'] in projects,
                   'scoped_not_found', 404)
            self._scoped_job(item['job_id'], principal, projects, recipes)
            row = self.db.execute('SELECT * FROM operations WHERE command_id=?', (command_id,)).fetchone()
            ensure(row is not None and row['job_id'] is None, 'scoped_journal_invalid', 503)
            saved = json.loads(row['payload'])
            ensure(saved.get('_receiver_principal') == principal and saved.get('_receiver_job') == item['job_id'],
                   'scoped_journal_invalid', 503)
            return self._view(row)

    def scoped_metadata(self, resource_id, principal, projects, recipes):
        with self.lock:
            self._scoped_resource(resource_id, principal, projects, recipes)
            return self.metadata(resource_id)

    def scoped_file(self, resource_id, name, principal, projects, recipes):
        with self.lock:
            self._scoped_resource(resource_id, principal, projects, recipes)
            return self.file(resource_id, name)

    def scoped_diagnostic(self, resource_id, digest, name, principal, projects, recipes):
        with self.lock:
            self._scoped_resource(resource_id, principal, projects, recipes)
            return self.diagnostic_file(resource_id, digest, name)

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

    def cleanup(self, payload, *, principal=None, projects=None, recipes=None):
        ensure(isinstance(payload, dict) and set(payload) == {'command_id', 'idempotency_key', 'resources'}, 'cleanup_schema')
        identifier(payload['command_id'])
        ensure(isinstance(payload['idempotency_key'], str) and 0 < len(payload['idempotency_key']) <= 128,
               'invalid_idempotency_key')
        ensure(isinstance(payload['resources'], list) and 0 < len(payload['resources']) <= 4, 'cleanup_resources')
        with self.lock:
            scoped_job = None
            old = self.db.execute('SELECT * FROM operations WHERE command_id=?', (payload['command_id'],)).fetchone()
            if principal is not None:
                self._scoped_schema()
                existing_owner = self.db.execute('SELECT * FROM scoped_cleanups WHERE command_id=?',
                                                 (payload['command_id'],)).fetchone()
                if old is not None:
                    ensure(existing_owner is not None and existing_owner['principal'] == principal
                           and existing_owner['project'] in projects, 'scoped_not_found', 404)
                    self._scoped_job(existing_owner['job_id'], principal, projects, recipes)
                    saved = json.loads(old['payload'])
                    ensure(saved.get('_receiver_principal') == principal and
                           saved.get('_receiver_job') == existing_owner['job_id'], 'scoped_journal_invalid', 503)
                    scoped_job = existing_owner
                else:
                    ensure(existing_owner is None, 'scoped_journal_invalid', 503)
                    for wanted in payload['resources']:
                        ensure(type(wanted) is dict and set(wanted) == {'resource_id', 'expected_revision'}, 'cleanup_resource_schema')
                        admission = self._scoped_resource(wanted['resource_id'], principal, projects, recipes)
                        if scoped_job is None:
                            scoped_job = admission
                        ensure(scoped_job['job_id'] == admission['job_id'], 'scoped_not_found', 404)
            elif old is not None:
                # Legacy credentials cannot replay or inspect a scoped cleanup
                # through this mutation route, even after a lost response.
                ensure('_receiver_principal' not in json.loads(old['payload']), 'scoped_not_found', 404)
            identity_scope = ({'_receiver_principal': principal, '_receiver_job': scoped_job['job_id']}
                              if principal is not None else None)
            identity, existing = self._operation(payload['command_id'], payload, identity_scope=identity_scope)
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
                if principal is None:
                    parent_op = self.db.execute('SELECT payload FROM operations WHERE job_id=?', (item['job_id'],)).fetchone()
                    ensure(parent_op is not None and '_receiver_principal' not in json.loads(parent_op['payload']),
                           'scoped_not_found', 404)
                    if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='scoped_jobs'").fetchone() is not None:
                        self._scoped_schema()
                        ensure(self.db.execute('SELECT 1 FROM scoped_jobs WHERE job_id=?', (item['job_id'],)).fetchone() is None,
                               'scoped_not_found', 404)
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
                saved = dict(payload)
                if principal is not None:
                    self._scoped_schema()
                    self._scoped_job(scoped_job['job_id'], principal, projects, recipes)
                    for wanted in payload['resources']:
                        admission = self._scoped_resource(wanted['resource_id'], principal, projects, recipes)
                        current_item = self.metadata(wanted['resource_id'])
                        ensure(admission['job_id'] == scoped_job['job_id'] and
                               current_item['revision'] == wanted['expected_revision'] and
                               current_item['state'] != 'deleted' and not current_item['refs'],
                               'cleanup_revision_or_reference', 409)
                    saved.update(_receiver_principal=principal, _receiver_job=scoped_job['job_id'])
                    self.db.execute('INSERT INTO scoped_cleanups VALUES (?,?,?,?)',
                        (payload['command_id'], principal, scoped_job['project'], scoped_job['job_id']))
                self.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',
                                (payload['command_id'], identity, None, 'running', json.dumps(saved), '{}'))
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
