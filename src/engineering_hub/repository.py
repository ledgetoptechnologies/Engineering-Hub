"""Local, transactional Hub state. This module never dispatches work."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid


PROJECTS = frozenset({'engineering-hub', '3d-viewer', 'project-alpha', 'ledge-top-ops', 'treedar'})
AGENTS = frozenset({'hermes', 'codex', 'claude'})


class HubError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


class HubRepository:
    def __init__(self, path, *, execution_principal=None):
        if execution_principal is not None and (
            not isinstance(execution_principal, str)
            or re.fullmatch(r'executor:[a-z][a-z0-9-]{0,63}', execution_principal) is None
        ):
            raise HubError('invalid_execution_principal')
        self.execution_principal = execution_principal
        self.path = str(path)
        if self.path == ':memory:':
            raise HubError('durable_path_required')
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS hub_entities (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hub_requests (
                    actor TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    entity_id TEXT NOT NULL, PRIMARY KEY(actor,key));
                CREATE TABLE IF NOT EXISTS hub_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL,
                    action TEXT NOT NULL, entity_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hub_outbox (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
                    entity_id TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hub_events (
                    run_id TEXT NOT NULL, command_id TEXT NOT NULL, event TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, PRIMARY KEY(run_id,command_id,event));
                CREATE TABLE IF NOT EXISTS hub_target_fences (
                    target TEXT PRIMARY KEY, generation INTEGER NOT NULL CHECK(generation > 0));
            ''')

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    def close(self):
        """Connections are scoped to each operation; no persistent handle exists."""

    @staticmethod
    def _actor(actor, human=False, agent=False):
        valid_human = isinstance(actor, str) and re.fullmatch(r'human:[1-9][0-9]{0,19}', actor) is not None
        if not (actor in AGENTS or valid_human) or (human and not valid_human) or (agent and actor not in AGENTS):
            raise HubError('forbidden', 403)

    @staticmethod
    def _text(value, maximum=200):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise HubError('invalid_input')
        return value.strip()

    @staticmethod
    def _project(project):
        if project not in PROJECTS:
            raise HubError('invalid_project')

    @staticmethod
    def _get(db, identity, kind):
        row = db.execute('SELECT data FROM hub_entities WHERE id=? AND kind=?', (identity, kind)).fetchone()
        if row is None:
            raise HubError('not_found', 404)
        return json.loads(row['data'])

    @staticmethod
    def _cas(item, expected):
        if type(expected) is not int or item['revision'] != expected:
            raise HubError('revision_conflict', 409)

    def _save(self, db, actor, action, kind, item, new=False):
        item['revision'] = 1 if new else item['revision'] + 1
        item['updated_at'] = datetime.now(timezone.utc).isoformat()
        db.execute('INSERT INTO hub_entities(id,kind,data) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                   (item['id'], kind, _json(item)))
        db.execute('INSERT INTO hub_audit(actor,action,entity_id,revision,created_at) VALUES(?,?,?,?,?)',
                   (actor, action, item['id'], item['revision'], item['updated_at']))
        self._emit(db, action, item)
        return item

    def _emit(self, db, action, item):
        db.execute('INSERT INTO hub_outbox(action,entity_id,revision,created_at) VALUES(?,?,?,?)',
                   (action, item['id'], item['revision'], item['updated_at']))

    def _list(self, kind):
        with self._connection() as db:
            return [json.loads(row['data']) for row in db.execute('SELECT data FROM hub_entities WHERE kind=? ORDER BY rowid', (kind,))]

    def _read(self, identity, kind):
        with self._connection() as db:
            return self._get(db, identity, kind)

    def create_task(self, actor, project, title, client_request_id):
        self._actor(actor, human=True)
        self._project(project)
        title, key = self._text(title), self._text(client_request_id)
        fingerprint = hashlib.sha256(_json([project, title]).encode()).hexdigest()
        with self._transaction() as db:
            prior = db.execute('SELECT * FROM hub_requests WHERE actor=? AND key=?', (actor, key)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise HubError('idempotency_conflict', 409)
                return self._get(db, prior['entity_id'], 'task')
            item = self._save(db, actor, 'task.created', 'task', dict(id=str(uuid.uuid4()), project=project,
                title=title, status='assigned', owner=None, candidate_commit=None, review=None, repair_count=0), new=True)
            db.execute('INSERT INTO hub_requests VALUES(?,?,?,?)', (actor, key, fingerprint, item['id']))
            return item

    def list_tasks(self):
        return self._list('task')

    def get_task(self, identity):
        return self._read(identity, 'task')

    def claim_task(self, actor, identity, expected_revision):
        self._actor(actor, agent=True)
        with self._transaction() as db:
            item = self._get(db, identity, 'task')
            self._cas(item, expected_revision)
            if item['status'] != 'assigned':
                raise HubError('already_claimed', 409)
            item.update(owner=actor, status='claimed')
            return self._save(db, actor, 'task.claimed', 'task', item)

    @staticmethod
    def _no_active_run(db, task_id, target=None):
        for row in db.execute("SELECT data FROM hub_entities WHERE kind='run'"):
            run = json.loads(row['data'])
            if (run['task_id'] == task_id or (target and run['target'] == target)) and run['status'] in {'pending', 'running', 'held', 'failed'}:
                raise HubError('active_run', 409)

    def set_candidate(self, actor, identity, expected_revision, commit):
        self._actor(actor, agent=True)
        if not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40}', commit):
            raise HubError('invalid_commit')
        with self._transaction() as db:
            item = self._get(db, identity, 'task')
            self._cas(item, expected_revision)
            if item['owner'] != actor:
                raise HubError('forbidden', 403)
            self._no_active_run(db, identity)
            item.update(candidate_commit=commit, review=None, status='in_review')
            return self._save(db, actor, 'task.candidate', 'task', item)

    def review_task(self, actor, identity, expected_revision, commit, verdict):
        self._actor(actor, agent=True)
        if verdict not in {'approve', 'reject'}:
            raise HubError('invalid_verdict')
        with self._transaction() as db:
            item = self._get(db, identity, 'task')
            self._cas(item, expected_revision)
            if item['owner'] == actor:
                raise HubError('independent_review_required', 403)
            if item['status'] != 'in_review' or not commit or item['candidate_commit'] != commit:
                raise HubError('stale_review', 409)
            item.update(status='verified' if verdict == 'approve' else 'claimed',
                        review=dict(actor=actor, commit=commit, verdict=verdict) if verdict == 'approve' else None)
            return self._save(db, actor, 'task.reviewed', 'task', item)

    def create_decision(self, actor, project, title, options, protected=False):
        self._actor(actor)
        self._project(project)
        title = self._text(title)
        if type(protected) is not bool or not isinstance(options, list) or not 2 <= len(options) <= 5:
            raise HubError('invalid_options')
        options = [self._text(option) for option in options]
        if len(set(options)) != len(options):
            raise HubError('invalid_options')
        with self._transaction() as db:
            return self._save(db, actor, 'decision.created', 'decision', dict(id=str(uuid.uuid4()), project=project,
                title=title, options=options, protected=protected, status='open', answer=None,
                execution_authorization=False), new=True)

    def list_decisions(self):
        return self._list('decision')

    def answer_decision(self, actor, identity, expected_revision, answer):
        self._actor(actor, human=True)
        with self._transaction() as db:
            item = self._get(db, identity, 'decision')
            self._cas(item, expected_revision)
            if item['status'] != 'open' or answer not in item['options']:
                raise HubError('invalid_answer', 409)
            item.update(answer=answer, answered_by=actor, status='answered')
            return self._save(db, actor, 'decision.answered', 'decision', item)

    def create_run(self, actor, task_id, expected_revision, artifact_digest, target, *, execution_manifest_digest=None):
        self._actor(actor, agent=True)
        if not isinstance(artifact_digest, str) or not re.fullmatch('sha256:[0-9a-f]{64}', artifact_digest):
            raise HubError('invalid_digest')
        if execution_manifest_digest is not None and (
            not isinstance(execution_manifest_digest, str)
            or re.fullmatch('sha256:[0-9a-f]{64}', execution_manifest_digest) is None
        ):
            raise HubError('invalid_manifest_digest')
        with self._transaction() as db:
            task = self._get(db, task_id, 'task')
            self._cas(task, expected_revision)
            if self._paused(db):
                raise HubError('paused', 409)
            if task['owner'] != actor:
                raise HubError('forbidden', 403)
            if target != task['project'] + ':staging':
                raise HubError('invalid_target', 403)
            if task['status'] != 'verified' or not task['review'] or task['review']['commit'] != task['candidate_commit']:
                raise HubError('review_required', 409)
            if task['repair_count'] >= 3:
                raise HubError('repair_limit', 409)
            self._no_active_run(db, task_id, target)
            fence = db.execute('SELECT generation FROM hub_target_fences WHERE target=?', (target,)).fetchone()
            # Include older run snapshots when upgrading a pre-fence Hub database.
            previous_generations = [fence['generation'] if fence else 0]
            for row in db.execute("SELECT data FROM hub_entities WHERE kind='run'"):
                previous_run = json.loads(row['data'])
                if previous_run['target'] == target:
                    previous_generations.append(previous_run['fencing_generation'])
            generation = max(previous_generations) + 1
            db.execute('INSERT INTO hub_target_fences(target,generation) VALUES(?,?) '
                       'ON CONFLICT(target) DO UPDATE SET generation=excluded.generation', (target, generation))
            run = self._save(db, actor, 'run.created', 'run', dict(id=str(uuid.uuid4()), task_id=task_id,
                project=task['project'], target=target, commit=task['candidate_commit'], artifact_digest=artifact_digest,
                policy_revision=1, review=task['review'], status='pending', command_id=None, fencing_generation=generation,
                created_by=actor, execution_actor=self.execution_principal,
                execution_manifest_digest=execution_manifest_digest, events=[]), new=True)
            self._save(db, actor, 'task.run_created', 'task', task)
            return run

    def get_run(self, identity):
        return self._read(identity, 'run')

    def list_runs(self):
        return self._list('run')

    def append_run_event(self, actor, runid, event, command_id, fencing_generation, expected_revision):
        if self.execution_principal is None or actor != self.execution_principal:
            raise HubError('forbidden', 403)
        command_id = self._text(command_id)
        if event not in {'running', 'succeeded', 'failed', 'rolled_back', 'held'} or type(fencing_generation) is not int or fencing_generation < 1:
            raise HubError('invalid_event')
        fingerprint = _json([actor, fencing_generation])
        with self._transaction() as db:
            run = self._get(db, runid, 'run')
            if run['execution_actor'] != actor:
                raise HubError('forbidden', 403)
            if fencing_generation != run['fencing_generation']:
                raise HubError('stale_fence', 409)
            if run['command_id'] is not None and run['command_id'] != command_id:
                raise HubError('command_conflict', 409)
            previous = db.execute('SELECT fingerprint FROM hub_events WHERE run_id=? AND command_id=? AND event=?', (runid, command_id, event)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise HubError('event_conflict', 409)
                return run
            self._cas(run, expected_revision)
            transitions = {'pending': {'running', 'held', 'failed'}, 'running': {'succeeded', 'failed', 'held'},
                           'held': {'failed'}, 'failed': {'rolled_back'}}
            if event not in transitions.get(run['status'], set()):
                raise HubError('invalid_transition', 409)
            if self._paused(db) and event == 'running':
                raise HubError('paused', 409)
            run.update(status=event, command_id=command_id, fencing_generation=fencing_generation, execution_actor=actor)
            run['events'].append(dict(event=event, actor=actor, command_id=command_id, fencing_generation=fencing_generation))
            db.execute('INSERT INTO hub_events VALUES(?,?,?,?)', (runid, command_id, event, fingerprint))
            if event == 'failed':
                task = self._get(db, run['task_id'], 'task')
                task['repair_count'] = min(3, task['repair_count'] + 1)
                self._save(db, actor, 'task.run_failed', 'task', task)
            return self._save(db, actor, 'run.' + event, 'run', run)

    @staticmethod
    def _paused(db):
        row = db.execute("SELECT data FROM hub_entities WHERE id='pause' AND kind='policy'").fetchone()
        return json.loads(row['data'])['paused'] if row else False

    def get_paused(self):
        with self._connection() as db:
            return self._paused(db)

    def set_pause(self, actor, paused):
        self._actor(actor, human=True)
        if type(paused) is not bool:
            raise HubError('invalid_input')
        with self._transaction() as db:
            row = db.execute("SELECT data FROM hub_entities WHERE id='pause'").fetchone()
            item = json.loads(row['data']) if row else dict(id='pause', revision=0)
            item['paused'] = paused
            return self._save(db, actor, 'policy.paused', 'policy', item)

    def _metadata(self, table, limit):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise HubError('invalid_limit')
        with self._connection() as db:
            return [dict(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY sequence LIMIT ?', (limit,))]

    def list_outbox(self, limit=50):
        return self._metadata('hub_outbox', limit)

    def list_audit(self, limit=50):
        return self._metadata('hub_audit', limit)
