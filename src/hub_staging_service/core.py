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


class Service:
    def __init__(self, root, projects, backend, *, capacity_bytes=1024 * 1024 * 1024,
                 free_floor_bytes=512 * 1024 * 1024):
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
        ''')
        self.db.execute("UPDATE operations SET state='held' WHERE state IN ('running','accepted')")
        self.db.commit()

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
            ensure(not self.db.execute('SELECT 1 FROM operations WHERE job_id=?', (payload['job_id'],)).fetchone(), 'job_conflict', 409)
            for resource_id in ids:
                ensure(not self.db.execute('SELECT 1 FROM resources WHERE id=?', (resource_id,)).fetchone()
                       and not (self.resources / resource_id).exists(), 'resource_conflict', 409)
            reservation = MAX_EXPANDED + 3 * profile['output_bytes'] + len(raw)
            self._capacity(reservation)
            self.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',
                            (payload['command_id'], identity, payload['job_id'], 'accepted', json.dumps(payload), '{}'))
            self.db.commit()
            self.active = payload['command_id']
            self.thread = threading.Thread(target=self._execute, args=(payload, raw), daemon=False)
            try:
                self.thread.start()
            except Exception:
                self.db.execute("UPDATE operations SET state='held' WHERE command_id=?", (self.active,))
                self.db.commit(); self.active = None
                raise Rejected('worker_start_failed', 503) from None
            return self.status(payload['job_id'])

    def _execute(self, payload, raw):
        try:
            with self.lock:
                self.db.execute("UPDATE operations SET state='running' WHERE command_id=?", (payload['command_id'],))
                self.db.commit()
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
            # Rootless Docker maps host owner to container root. Read-only
            # source must also be readable by the explicitly non-root job UID;
            # the private worker-root ancestor still prevents host-wide access.
            for directory, dirs, files in os.walk(paths['workspace'], followlinks=False):
                Path(directory).chmod(0o755)
                for name in files:
                    (Path(directory) / name).chmod(0o644)
            profile = self.projects[payload['project']][payload['recipe']]
            result = self.backend.run(paths, profile, payload['command_id'])
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
            result = {'error': exc.code if isinstance(exc, Rejected) else 'execution_uncertain'}
            state = 'held' if result['error'] == 'execution_uncertain' else 'failed'
        with self.lock:
            self.db.execute('UPDATE operations SET state=?,result=? WHERE command_id=?',
                            (state, json.dumps(result), payload['command_id']))
            self.db.commit(); self.active = None

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

    def cleanup(self, payload):
        ensure(isinstance(payload, dict) and set(payload) == {'command_id', 'idempotency_key', 'resources'}, 'cleanup_schema')
        identifier(payload['command_id'])
        ensure(isinstance(payload['idempotency_key'], str) and 0 < len(payload['idempotency_key']) <= 128,
               'invalid_idempotency_key')
        ensure(isinstance(payload['resources'], list) and 0 < len(payload['resources']) <= 4, 'cleanup_resources')
        with self.lock:
            identity, existing = self._operation(payload['command_id'], payload)
            if existing:
                return existing
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
