"""Local, transactional Hub state. This module never dispatches work."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

from .pilot_ownership import PilotOwner
from .image_state_boundary import ImageStateBoundaryError, require_image_state_mount
from .entity_context import bounded_text, validated_context


PROJECTS = frozenset({'engineering-hub', '3d-viewer', 'project-alpha', 'ledge-top-ops', 'treedar'})
AGENTS = frozenset({'hermes', 'codex', 'claude'})
# Audit actions that set a task's status. Run bookkeeping saves the task without
# changing it, so those actions must not reset how long a task has been waiting.
TASK_STATUS_ACTIONS = ('task.created', 'task.proposed', 'task.claimed', 'task.candidate', 'task.reviewed',
                       'task.context_invalidated')
HELD_REASONS = frozenset({'candidate_outcome_uncertain', 'staging_paused', 'rollback_failed',
                         'rollback_outcome_uncertain', 'rollback_smoke_failed', 'rollback_smoke_outcome_uncertain'})


class HubError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


class HubRepository:
    def __init__(self, path, *, execution_principal=None, pilot_owner=None):
        if execution_principal is not None and (
            not isinstance(execution_principal, str)
            or re.fullmatch(r'executor:[a-z][a-z0-9-]{0,63}', execution_principal) is None
        ):
            raise HubError('invalid_execution_principal')
        self.execution_principal = execution_principal
        self.pilot_owner = pilot_owner
        self.path = str(path)
        if self.path == ':memory:':
            raise HubError('durable_path_required')
        try:
            require_image_state_mount(self.path)
        except ImageStateBoundaryError:
            raise HubError('hub_image_state_mount_required',409) from None
        self._assert_pilot_owner(None)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._write_connection() as db:
            # Even WAL configuration is a mutation. A legacy constructor must
            # not change an enrolled ledger before checking its live owner.
            self._assert_pilot_owner(db)
            db.execute('PRAGMA journal_mode=WAL')
        with self._transaction() as db:
            schema = '''
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
                CREATE TABLE IF NOT EXISTS hub_run_event_receipts (
                    run_id TEXT NOT NULL, event_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    PRIMARY KEY(run_id,event_id));
                CREATE TABLE IF NOT EXISTS hub_target_fences (
                    target TEXT PRIMARY KEY, generation INTEGER NOT NULL CHECK(generation > 0));
                CREATE TABLE IF NOT EXISTS hub_run_requests (
                    actor TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    run_id TEXT NOT NULL, PRIMARY KEY(actor,key));
            '''
            # executescript commits an existing transaction implicitly, which
            # would bypass the final capability check and rollback boundary.
            for statement in schema.split(';'):
                if statement.strip():
                    db.execute(statement)

    def _assert_pilot_owner(self, db):
        """Require the actual live capability for any enrolled-ledger write."""
        try:
            if self.pilot_owner is not None:
                if type(self.pilot_owner) is not PilotOwner:
                    raise ValueError()
                self.pilot_owner.assert_active(self.path)
            elif db is not None and db.execute("SELECT 1 FROM sqlite_master WHERE name='hub_pilot_owner'").fetchone():
                raise ValueError()
        except Exception:
            raise HubError('pilot_ownership_required', 409) from None

    @contextmanager
    def _connection(self):
        """Read contexts cannot mutate an enrolled ledger, even for its owner."""
        if self.pilot_owner is not None:
            db = sqlite3.connect(Path(self.path).absolute().as_uri() + '?mode=ro', uri=True, timeout=10)
        elif Path(self.path).exists():
            # Probe existing ledgers read-only as well: an initial RW open or
            # schema read must not recover a journal on an enrolled database.
            db = sqlite3.connect(Path(self.path).absolute().as_uri() + '?mode=ro', uri=True, timeout=10)
            # Preserve historical un-enrolled fixture behavior only. Do not
            # hand out a writable handle after discovering enrollment, even
            # when this repository was constructed before that marker existed.
            try:
                enrolled = db.execute("SELECT 1 FROM sqlite_master WHERE name='hub_pilot_owner'").fetchone()
            except BaseException:
                db.close()
                raise
            if not enrolled:
                db.close()
                db = sqlite3.connect(self.path, timeout=10)
        else:
            db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _write_connection(self):
        """Private writer boundary; only initialization and transactions use it."""
        self._assert_pilot_owner(None)
        if self.pilot_owner is not None:
            # Enrollment already created this file. Never recreate it on loss.
            db = sqlite3.connect(Path(self.path).absolute().as_uri() + '?mode=rw', uri=True, timeout=10)
        else:
            db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            self._assert_pilot_owner(db)
            yield db
            self._assert_pilot_owner(db)
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        self._assert_pilot_owner(None)
        with self._write_connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                self._assert_pilot_owner(db)
                yield db
                self._assert_pilot_owner(db)
                db.commit()
            except Exception:
                db.rollback()
                raise

    def close(self):
        """Connections are scoped to each operation; no persistent handle exists."""

    def queued_pilot_enrolled(self, db=None):
        """Persistent exclusion, including for objects created before enrollment."""
        if db is None:
            with self._connection() as connection:
                return self.queued_pilot_enrolled(connection)
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='hub_queued_pilot_policy'").fetchone() is not None

    def _assert_queued_pilot(self, operation, *, db=None, **context):
        if not self.queued_pilot_enrolled(db):
            return
        # The exact trusted local bridge is not a bearer identity, request flag
        # or callback supplied by a public caller. Its private scoped permit
        # rechecks the original job, live owner and physical reservation.
        from .queued_pilot import QueuedContainerPilot
        bridge = getattr(self, '_queued_pilot', None)
        if (type(bridge) is not QueuedContainerPilot or bridge.repo is not self
                or bridge.authorize(operation, db=db, **context) is not True):
            raise HubError('queued_pilot_admission_required', 409)

    @staticmethod
    def _actor(actor, human=False, agent=False):
        valid_human = isinstance(actor, str) and re.fullmatch(r'human:(?:[1-9][0-9]{0,19}|cf:[0-9a-f]{64})', actor) is not None
        if not (actor in AGENTS or valid_human) or (human and not valid_human) or (agent and actor not in AGENTS):
            raise HubError('forbidden', 403)

    @staticmethod
    def _text(value, maximum=200):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise HubError('invalid_input')
        return value.strip()

    def _project(self, project, db=None, *, require_active=True):
        """Validate metadata scope, never an execution target or permission grant."""
        if type(project) is not str or re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', project) is None:
            raise HubError('invalid_project')
        if db is None:
            with self._connection() as connection:
                return self._project(project, connection, require_active=require_active)
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_projects'").fetchone():
            if project not in PROJECTS:
                raise HubError('invalid_project')
            return
        row = db.execute('SELECT data FROM hub_projects WHERE id=?', (project,)).fetchone()
        if row is None:
            raise HubError('invalid_project')
        if require_active and json.loads(row['data']).get('archived', False):
            raise HubError('project_archived', 409)

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

    @staticmethod
    def _context(details, links):
        try:
            return validated_context(details, links)
        except ValueError:
            raise HubError('invalid_context') from None

    def create_task(self, actor, project, title, client_request_id, *, details=None, links=None):
        self._actor(actor, human=True)
        title, key = self._text(title), self._text(client_request_id)
        context = self._context(details, links)
        payload = [project, title] + ([context] if context else [])
        fingerprint = hashlib.sha256(_json(payload).encode()).hexdigest()
        with self._transaction() as db:
            self._project(project, db)
            prior = db.execute('SELECT * FROM hub_requests WHERE actor=? AND key=?', (actor, key)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise HubError('idempotency_conflict', 409)
                return self._get(db, prior['entity_id'], 'task')
            item = self._save(db, actor, 'task.created', 'task', dict(id=str(uuid.uuid4()), project=project,
                title=title, status='assigned', owner=None, candidate_commit=None, review=None, repair_count=0,
                **context), new=True)
            db.execute('INSERT INTO hub_requests VALUES(?,?,?,?)', (actor, key, fingerprint, item['id']))
            return item

    def list_tasks(self):
        return self._list('task')

    def list_task_views(self):
        """Response copies of tasks plus the audited time each entered its current status.

        state_since is derived from the audit log, never stored on the task, and is
        None when no status action was recorded. Tasks and audit rows are read in one
        snapshot, and the status action is chosen by revision (never wall-clock order)
        bounded by each returned task's revision, so a concurrent write or a clock
        change cannot attach a newer time to an older status.
        """
        marks = ','.join('?' * len(TASK_STATUS_ACTIONS))
        with self._connection() as db:
            db.execute('BEGIN')
            try:
                tasks = [json.loads(row['data']) for row in db.execute(
                    "SELECT data FROM hub_entities WHERE kind='task' ORDER BY rowid")]
                audit = db.execute(
                    f'SELECT entity_id, revision, created_at FROM hub_audit WHERE action IN ({marks})',
                    TASK_STATUS_ACTIONS).fetchall()
            finally:
                db.rollback()
        bound = {task['id']: task['revision'] for task in tasks}
        best = {}
        for row in audit:
            limit = bound.get(row['entity_id'])
            if limit is None or row['revision'] > limit:
                continue
            if row['entity_id'] not in best or row['revision'] > best[row['entity_id']][0]:
                best[row['entity_id']] = (row['revision'], row['created_at'])
        return [dict(task, state_since=best[task['id']][1] if task['id'] in best else None) for task in tasks]

    def propose_staging_task(self, actor, project, title, client_request_id, *, details=None, links=None):
        """An agent may propose work, never grant execution or production authority."""
        self._actor(actor, agent=True)
        title, key = self._text(title), self._text(client_request_id, 128)
        context = self._context(details, links)
        payload = ['staging_task_proposal', project, title] + ([context] if context else [])
        fingerprint = hashlib.sha256(_json(payload).encode()).hexdigest()
        with self._transaction() as db:
            self._project(project, db)
            prior = db.execute('SELECT * FROM hub_requests WHERE actor=? AND key=?', (actor, key)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise HubError('idempotency_conflict', 409)
                return self._get(db, prior['entity_id'], 'task')
            item = self._save(db, actor, 'task.proposed', 'task', dict(
                id=str(uuid.uuid4()), project=project, title=title, status='assigned',
                owner=None, candidate_commit=None, review=None, repair_count=0,
                scope='staging', proposed_by=actor, **context), new=True)
            db.execute('INSERT INTO hub_requests VALUES(?,?,?,?)', (actor, key, fingerprint, item['id']))
            return item

    def task_allows_environment(self, task_id, environment, *, db=None):
        """Persisted proposal scope is an unconditional veto, not an approval.

        Legacy taskless coordination targets retain their existing semantics.
        Scope is never accepted from a queue caller or changed by task updates.
        """
        if db is None:
            with self._connection() as connection:
                return self.task_allows_environment(task_id, environment, db=connection)
        row = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='task'", (task_id,)).fetchone()
        if row is None:
            return True
        task = json.loads(row['data'])
        try:
            self._project(task.get('project'), db)
        except HubError:
            return False
        if 'scope' not in task and 'proposed_by' not in task:
            return True
        return (task.get('scope') == 'staging' and type(task.get('proposed_by')) is str
                and task['proposed_by'] in AGENTS
                and environment == 'staging')

    def get_task(self, identity):
        return self._read(identity, 'task')

    def claim_task(self, actor, identity, expected_revision):
        self._actor(actor, agent=True)
        with self._transaction() as db:
            item = self._get(db, identity, 'task')
            self._project(item['project'], db)
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
            self._project(item['project'], db)
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
            self._project(item['project'], db)
            self._cas(item, expected_revision)
            if item['owner'] == actor:
                raise HubError('independent_review_required', 403)
            if item['status'] != 'in_review' or not commit or item['candidate_commit'] != commit:
                raise HubError('stale_review', 409)
            item.update(status='verified' if verdict == 'approve' else 'claimed',
                        review=dict(actor=actor, commit=commit, verdict=verdict) if verdict == 'approve' else None)
            return self._save(db, actor, 'task.reviewed', 'task', item)

    def create_decision(self, actor, project, title, options, protected=False, *, details=None, links=None, allow_text=False):
        self._actor(actor)
        title = self._text(title)
        if type(protected) is not bool or not isinstance(options, list) or not 2 <= len(options) <= 5:
            raise HubError('invalid_options')
        options = [self._text(option) for option in options]
        if len(set(options)) != len(options):
            raise HubError('invalid_options')
        if type(allow_text) is not bool:
            raise HubError('invalid_options')
        context = self._context(details, links)
        if allow_text:
            context['allow_text'] = True
        with self._transaction() as db:
            self._project(project, db)
            return self._save(db, actor, 'decision.created', 'decision', dict(id=str(uuid.uuid4()), project=project,
                title=title, options=options, protected=protected, status='open', answer=None,
                execution_authorization=False, **context), new=True)

    def update_context(self, actor, kind, identity, expected_revision, details, links):
        """Human-only CAS edit; task review never survives changed instructions."""
        self._actor(actor, human=True)
        if kind not in {'task', 'decision'}:
            raise HubError('invalid_kind')
        context = self._context(details, links)
        with self._transaction() as db:
            item = self._get(db, identity, kind)
            self._project(item['project'], db)
            self._cas(item, expected_revision)
            action = kind + '.context_updated'
            if kind == 'decision':
                if item['status'] != 'open':
                    raise HubError('decision_closed', 409)
            else:
                # A queued manifest/review may already bind the task revision.
                # Do not mutate that admission surface through this new API.
                if self.queued_pilot_enrolled(db):
                    raise HubError('queued_pilot_admission_required', 409)
                self._no_active_run(db, identity)
                if item.get('candidate_commit') is not None or item.get('review') is not None:
                    item.update(candidate_commit=None, review=None,
                                status='claimed' if item.get('owner') else 'assigned')
                    action = 'task.context_invalidated'
            for field in ('details', 'links'):
                item.pop(field, None)
            item.update(context)
            return self._save(db, actor, action, kind, item)

    def list_decisions(self):
        return self._list('decision')

    def answer_decision(self, actor, identity, expected_revision, answer):
        self._actor(actor, human=True)
        with self._transaction() as db:
            item = self._get(db, identity, 'decision')
            self._project(item['project'], db)
            self._cas(item, expected_revision)
            if item.get('allow_text') is True:
                try:
                    answer = bounded_text(answer, 4096)
                except ValueError:
                    raise HubError('invalid_answer', 409) from None
            if item['status'] != 'open' or (item.get('allow_text') is not True and answer not in item['options']):
                raise HubError('invalid_answer', 409)
            item.update(answer=answer, answered_by=actor, status='answered')
            return self._save(db, actor, 'decision.answered', 'decision', item)

    def create_run(self, actor, task_id, expected_revision, artifact_digest, target, *, execution_manifest_digest=None, client_request_id=None):
        self._actor(actor, agent=True)
        if not isinstance(artifact_digest, str) or not re.fullmatch('sha256:[0-9a-f]{64}', artifact_digest):
            raise HubError('invalid_digest')
        if execution_manifest_digest is not None and (
            not isinstance(execution_manifest_digest, str)
            or re.fullmatch('sha256:[0-9a-f]{64}', execution_manifest_digest) is None
        ):
            raise HubError('invalid_manifest_digest')
        key = self._text(client_request_id, 128) if client_request_id is not None else None
        fingerprint = hashlib.sha256(_json([task_id, expected_revision, artifact_digest, target, execution_manifest_digest]).encode()).hexdigest()
        with self._transaction() as db:
            self._assert_queued_pilot('create_run', db=db, actor=actor,
                task_id=task_id, expected_revision=expected_revision,
                artifact_digest=artifact_digest, target=target,
                execution_manifest_digest=execution_manifest_digest,
                client_request_id=client_request_id)
            if key is not None:
                prior = db.execute('SELECT fingerprint,run_id FROM hub_run_requests WHERE actor=? AND key=?', (actor, key)).fetchone()
                if prior:
                    if prior['fingerprint'] != fingerprint:
                        raise HubError('idempotency_conflict', 409)
                    return self._get(db, prior['run_id'], 'run')
            task = self._get(db, task_id, 'task')
            self._project(task['project'], db)
            self._cas(task, expected_revision)
            if self._paused(db):
                raise HubError('paused', 409)
            if task['owner'] != actor:
                raise HubError('forbidden', 403)
            if target != task['project'] + ':staging':
                raise HubError('invalid_target', 403)
            # Recheck the evidence at consumption, not merely the stored status:
            # legacy/corrupted rows must never allocate execution authority.
            review, candidate = task.get('review'), task.get('candidate_commit')
            if (task['status'] != 'verified' or not isinstance(review, dict)
                or review.get('verdict') != 'approve'
                or not isinstance(review.get('actor'), str) or review['actor'] not in AGENTS
                or review['actor'] == task['owner']
                or not isinstance(candidate, str) or re.fullmatch('[0-9a-f]{40}', candidate) is None
                or review.get('commit') != candidate):
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
            if key is not None:
                self._record_run_request(db, actor, key, fingerprint, run['id'])
            return run

    @staticmethod
    def _record_run_request(db, actor, key, fingerprint, run_id):
        db.execute('INSERT INTO hub_run_requests(actor,key,fingerprint,run_id) VALUES(?,?,?,?)',
                   (actor, key, fingerprint, run_id))

    def get_run_request(self, actor, key):
        self._actor(actor, agent=True)
        key = self._text(key, 128)
        with self._connection() as db:
            receipt = db.execute('SELECT run_id FROM hub_run_requests WHERE actor=? AND key=?', (actor, key)).fetchone()
            return self._get(db, receipt['run_id'], 'run') if receipt else None

    def get_run(self, identity):
        return self._read(identity, 'run')

    def list_runs(self):
        return self._list('run')

    def append_run_event(self, actor, runid, event, command_id, fencing_generation, expected_revision, *, reason=None, event_id=None):
        if self.execution_principal is None or actor != self.execution_principal:
            raise HubError('forbidden', 403)
        command_id = self._text(command_id)
        if event not in {'running', 'succeeded', 'failed', 'rolled_back', 'held'} or type(fencing_generation) is not int or fencing_generation < 1:
            raise HubError('invalid_event')
        if reason is not None and (event != 'held' or not isinstance(reason, str) or reason not in HELD_REASONS):
            raise HubError('invalid_event')
        if event_id is not None:
            try:
                if not isinstance(event_id, str) or str(uuid.UUID(event_id)) != event_id:
                    raise ValueError('noncanonical_event_id')
            except (ValueError, TypeError, AttributeError):
                raise HubError('invalid_event') from None
        # Preserve the exact legacy fingerprint for omitted reasons.
        if event_id is None:
            fingerprint = _json([actor, fencing_generation] if reason is None else [actor, fencing_generation, reason])
        else:
            fingerprint = _json([actor, command_id, event, fencing_generation, reason])
        with self._transaction() as db:
            run = self._get(db, runid, 'run')
            if run['execution_actor'] != actor:
                raise HubError('forbidden', 403)
            if fencing_generation != run['fencing_generation']:
                raise HubError('stale_fence', 409)
            if run['command_id'] is not None and run['command_id'] != command_id:
                raise HubError('command_conflict', 409)
            if event_id is None:
                previous = db.execute('SELECT fingerprint FROM hub_events WHERE run_id=? AND command_id=? AND event=?', (runid, command_id, event)).fetchone()
            else:
                previous = db.execute('SELECT fingerprint FROM hub_run_event_receipts WHERE run_id=? AND event_id=?', (runid, event_id)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise HubError('event_conflict', 409)
                return run
            self._cas(run, expected_revision)
            transitions = {'pending': {'running', 'held', 'failed'}, 'running': {'succeeded', 'failed', 'held'},
                           'held': {'failed'}, 'failed': {'rolled_back', 'held'}}
            if event not in transitions.get(run['status'], set()):
                raise HubError('invalid_transition', 409)
            if self._paused(db) and event == 'running':
                raise HubError('paused', 409)
            run.update(status=event, command_id=command_id, fencing_generation=fencing_generation, execution_actor=actor)
            entry = dict(event=event, actor=actor, command_id=command_id, fencing_generation=fencing_generation)
            if reason is not None:
                entry['reason'] = reason
            if event_id is not None:
                entry['event_id'] = event_id
            run['events'].append(entry)
            if event_id is None:
                db.execute('INSERT INTO hub_events VALUES(?,?,?,?)', (runid, command_id, event, fingerprint))
            else:
                db.execute('INSERT INTO hub_run_event_receipts VALUES(?,?,?)', (runid, event_id, fingerprint))
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
