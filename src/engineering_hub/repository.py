"""Local, transactional Hub state. This module never dispatches work."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

from agent_message_bus import session_hub_context as session_context
from .pilot_ownership import PilotOwner
from .image_state_boundary import ImageStateBoundaryError, require_image_state_mount
from .entity_context import bounded_text, validated_context


PROJECTS = frozenset({'engineering-hub', '3d-viewer', 'project-alpha', 'ledge-top-ops', 'treedar'})
AGENTS = frozenset({'hermes', 'codex', 'claude'})
# Audit actions that set a task's status. Run bookkeeping saves the task without
# changing it, so those actions must not reset how long a task has been waiting.
TASK_STATUS_ACTIONS = ('task.created', 'task.proposed', 'task.claimed', 'task.claim_recovered', 'task.candidate', 'task.reviewed',
                       'task.context_invalidated', 'task.withdrawn')
# Audit actions that record who created a task or decision. Withdrawal by an agent
# is limited to the agent named here, read from the audit log, never from the record.
CREATION_ACTIONS = {'task': ('task.created', 'task.proposed'), 'decision': ('decision.created',)}
WITHDRAWABLE_TASK_STATUSES = frozenset({'assigned', 'claimed'})
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
HELD_REASONS = frozenset({'candidate_outcome_uncertain', 'staging_paused', 'rollback_failed',
                         'rollback_outcome_uncertain', 'rollback_smoke_failed', 'rollback_smoke_outcome_uncertain',
                         'disposable_cleanup_requires_quiescence', 'disposable_cleanup_outcome_uncertain'})


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
                session_context.before_commit(self.path, db)
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
            return None
        # The exact trusted local bridge is not a bearer identity, request flag
        # or callback supplied by a public caller. Its private scoped permit
        # rechecks the original job, live owner and physical reservation.
        from .queued_pilot import QueuedContainerPilot
        bridge = getattr(self, '_queued_pilot', None)
        if type(bridge) is not QueuedContainerPilot or bridge.repo is not self:
            raise HubError('queued_pilot_admission_required', 409)
        authorization = bridge.authorize(operation, db=db, **context)
        if operation == 'create_run':
            # The private bridge returns the exact queued job it validated.
            # That job owns this task already, so it must be exempt from the
            # generic "another job is in progress" guard below.
            if not isinstance(authorization, str):
                raise HubError('queued_pilot_admission_required', 409)
            return authorization
        if authorization is not True:
            raise HubError('queued_pilot_admission_required', 409)
        return None

    @staticmethod
    def _actor(actor, human=False, agent=False):
        installation = session_context.actor_allowed(actor)
        if installation and human:
            raise HubError('forbidden', 403)
        if installation:
            return
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
        session_context.check_project(project)
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
        session_context.provenance(db, db.execute("SELECT last_insert_rowid()").fetchone()[0])
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

    def tracking_tasks(self, project, limit, after, unfinished=False):
        """Read inert agent tracking records; none is an execution task."""
        if type(unfinished) is not bool:
            raise HubError('invalid_input', 422)
        with self._connection() as db:
            self._project(project, db)
            state = " AND json_extract(data, '$.status') IN ('open','completion_claimed')" if unfinished else ''
            rows = db.execute("SELECT data FROM hub_entities WHERE kind='tracking_task' AND json_extract(data, '$.project')=? AND id>?" + state + " ORDER BY id LIMIT ?",
                              (project, after or '', limit + 1)).fetchall()
            return [json.loads(row['data']) for row in rows]

    def project_task_page(self, project, limit, after):
        """Bounded project-only rows, with status audit selected by revision."""
        with self._connection() as db:
            self._project(project, db)
            db.execute('BEGIN')
            try:
                rows = db.execute("SELECT data FROM hub_entities WHERE kind='task' AND json_extract(data, '$.project')=? AND json_extract(data, '$.status')!='withdrawn' AND id>? ORDER BY id LIMIT ?",
                                  (project, after or '', limit + 1)).fetchall()
                tasks = [json.loads(row['data']) for row in rows]
                marks = ','.join('?' * len(TASK_STATUS_ACTIONS))
                for task in tasks:
                    audit = db.execute(f'SELECT created_at FROM hub_audit WHERE entity_id=? AND revision<=? AND action IN ({marks}) ORDER BY revision DESC LIMIT 1',
                                       (task['id'], task['revision'], *TASK_STATUS_ACTIONS)).fetchone()
                    task['state_since'] = audit['created_at'] if audit else None
                paused = self._paused(db)
                return tasks, paused
            finally:
                db.rollback()

    def propose_tracking_task(self, actor, project, title, client_request_id, *, details=None, links=None):
        """Idempotent agent-authored tracking only; creates no dispatch authority."""
        self._actor(actor, agent=True)
        title, key = self._text(title), self._text(client_request_id, 128)
        context = self._context(details, links)
        fingerprint = hashlib.sha256(_json(['tracking_task', project, title, context]).encode()).hexdigest()
        with self._transaction() as db:
            self._project(project, db)
            prior = db.execute('SELECT * FROM hub_requests WHERE actor=? AND key=?', (actor, key)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise HubError('idempotency_conflict', 409)
                return self._get(db, prior['entity_id'], 'tracking_task')
            item = self._save(db, actor, 'tracking_task.proposed', 'tracking_task', dict(
                id=str(uuid.uuid4()), project=project, title=title, status='open',
                created_by=actor, execution_authorization=False, updates=[], **context), new=True)
            db.execute('INSERT INTO hub_requests VALUES(?,?,?,?)', (actor, key, fingerprint, item['id']))
            return item

    def update_tracking_task(self, actor, identity, expected_revision, client_request_id, *,
                             progress, evidence_links, completion_claim=False):
        """Append attributed progress or an unverified completion claim using CAS."""
        self._actor(actor, agent=True)
        key = self._text(client_request_id, 128)
        progress = self._text(progress, 4096)
        if type(completion_claim) is not bool:
            raise HubError('invalid_input')
        context = self._context(None, evidence_links)
        links = context.get('links', [])
        if completion_claim and not links:
            raise HubError('evidence_required')
        fingerprint = hashlib.sha256(_json(['tracking_update', identity, expected_revision,
                                             progress, links, completion_claim]).encode()).hexdigest()
        with self._transaction() as db:
            item = self._get(db, identity, 'tracking_task')
            self._project(item['project'], db)
            prior = db.execute('SELECT * FROM hub_requests WHERE actor=? AND key=?', (actor, key)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise HubError('idempotency_conflict', 409)
                return self._get(db, prior['entity_id'], 'tracking_task')
            self._cas(item, expected_revision)
            if item['status'] == 'completion_verified':
                raise HubError('tracking_reopen_required', 409)
            if len(item['updates']) >= 50:
                raise HubError('tracking_history_full', 409)
            item['updates'].append(dict(actor=actor, progress=progress, evidence_links=links,
                                        completion_claim=completion_claim, actor_family=session_context.family(actor),
                                        revision=expected_revision + 1))
            item['status'] = 'completion_claimed' if completion_claim else 'open'
            item = self._save(db, actor, 'tracking_task.updated', 'tracking_task', item)
            db.execute('INSERT INTO hub_requests VALUES(?,?,?,?)', (actor, key, fingerprint, item['id']))
            return item

    def review_tracking_task(self, actor, identity, expected_revision, client_request_id, *,
                             verdict, note, evidence_links):
        """Record an independent review of the exact latest claim, or reopen it."""
        self._actor(actor)
        key, note = self._text(client_request_id, 128), self._text(note, 4096)
        if verdict not in ('verify', 'reopen'):
            raise HubError('invalid_input')
        links = self._context(None, evidence_links).get('links', [])
        if verdict == 'verify' and not links:
            raise HubError('evidence_required')
        fingerprint = hashlib.sha256(_json(['tracking_review', identity, expected_revision,
                                             verdict, note, links]).encode()).hexdigest()
        with self._transaction() as db:
            item = self._get(db, identity, 'tracking_task')
            self._project(item['project'], db)
            prior = db.execute('SELECT * FROM hub_requests WHERE actor=? AND key=?', (actor, key)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise HubError('idempotency_conflict', 409)
                return self._get(db, prior['entity_id'], 'tracking_task')
            self._cas(item, expected_revision)
            if item['status'] not in (('completion_claimed',) if verdict == 'verify' else ('completion_claimed', 'completion_verified')):
                raise HubError('tracking_review_unavailable', 409)
            claim = item['updates'][-1] if item['updates'] else None
            if not claim or claim.get('completion_claim') is not True:
                raise HubError('tracking_review_unavailable', 409)
            # Old installation claims without retained family cannot be safely
            # shown independent of another installation; a human may review.
            if not actor.startswith('human:'):
                family = claim.get('actor_family')
                if family is None and claim.get('actor') in AGENTS:
                    family = claim['actor']
                if (not family or actor == claim.get('actor') or session_context.family(actor) == family):
                    raise HubError('independent_review_required', 403)
            if verdict == 'verify' and any(link not in claim.get('evidence_links', []) for link in links):
                raise HubError('tracking_evidence_mismatch', 409)
            reviews = item.setdefault('reviews', [])
            if len(reviews) >= 50:
                raise HubError('tracking_history_full', 409)
            reviews.append(dict(actor=actor, actor_family=session_context.family(actor), verdict=verdict,
                                note=note, evidence_links=links,
                                claim_revision=claim.get('revision', expected_revision if item['status'] == 'completion_claimed'
                                                         else reviews[-1]['claim_revision'])))
            item['status'] = 'completion_verified' if verdict == 'verify' else 'open'
            item = self._save(db, actor, 'tracking_task.reviewed', 'tracking_task', item)
            db.execute('INSERT INTO hub_requests VALUES(?,?,?,?)', (actor, key, fingerprint, item['id']))
            return item

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
                scope='staging', proposed_by=actor, **({'proposed_family': session_context.family(actor)} if session_context.actor_allowed(actor) else {}), **context), new=True)
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
            if db.execute("SELECT 1 FROM hub_entities WHERE id=? AND kind='tracking_task'", (task_id,)).fetchone():
                return False
            return True
        task = json.loads(row['data'])
        if task.get('status') == 'withdrawn':
            return False
        try:
            self._project(task.get('project'), db)
        except HubError:
            return False
        if 'scope' not in task and 'proposed_by' not in task:
            return True
        return (task.get('scope') == 'staging' and type(task.get('proposed_by')) is str
                and (task['proposed_by'] in AGENTS or (str(task['proposed_by']).startswith('installation:') and task.get('proposed_family') in AGENTS))
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
            item.update(owner=actor, status='claimed', **session_context.claim_fields(actor))
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
            if item['owner'] != actor or not session_context.continuation_allowed(item, actor):
                raise HubError('forbidden', 403)
            if item['status'] == 'withdrawn':
                raise HubError('withdrawn', 409)
            self._no_active_run(db, identity)
            if self._open_work_job(db, identity):
                raise HubError('active_work_job', 409)
            item.update(candidate_commit=commit, review=None, status='in_review',
                        **({'candidate_family': item['owner_family']} if 'owner_family' in item else {}))
            return self._save(db, actor, 'task.candidate', 'task', item)

    def review_task(self, actor, identity, expected_revision, commit, verdict):
        self._actor(actor, agent=True)
        if verdict not in {'approve', 'reject'}:
            raise HubError('invalid_verdict')
        with self._transaction() as db:
            item = self._get(db, identity, 'task')
            self._project(item['project'], db)
            self._cas(item, expected_revision)
            if self._open_work_job(db, identity):
                raise HubError('active_work_job', 409)
            if (item['owner'] == actor
                    or session_context.family(actor) == item.get('candidate_family', item.get('owner_family', item['owner']))):
                raise HubError('independent_review_required', 403)
            if item['status'] != 'in_review' or not commit or item['candidate_commit'] != commit:
                raise HubError('stale_review', 409)
            item.update(status='verified' if verdict == 'approve' else 'claimed',
                        review=dict(actor=actor, commit=commit, verdict=verdict,
                            **({'family': session_context.family(actor)} if session_context.actor_allowed(actor) else {})) if verdict == 'approve' else None)
            return self._save(db, actor, 'task.reviewed', 'task', item)

    def reviewed_task_matches(self, identity, actor, revision, commit, *, db=None):
        """Check the exact independently reviewed candidate while holding the queue transaction."""
        if db is None:
            with self._connection() as connection:
                return self.reviewed_task_matches(identity, actor, revision, commit, db=connection)
        try:
            task = self._get(db, identity, 'task')
        except HubError:
            return False
        return self._reviewed_task_data_matches(task, actor, revision, commit, db=db)

    def _reviewed_task_data_matches(self, task, actor, revision, commit, *, db=None):
        """Validate an exact review and its persisted per-installation lineage.

        Legacy agent identities are self-describing. Installation actors are
        accepted only when the task's candidate and review audit events have
        matching Hub-owned session provenance in the same database snapshot.
        Actor/family strings in the task JSON alone are never sufficient.
        """
        review = task.get('review')
        if not (type(revision) is int and task.get('revision') == revision
                and task.get('status') == 'verified' and task.get('owner') == actor
                and task.get('candidate_commit') == commit and type(review) is dict
                and review.get('verdict') == 'approve' and review.get('commit') == commit
                and type(review.get('actor')) is str and review.get('actor') != actor):
            return False
        candidate_family = task.get('candidate_family', task.get('owner_family', task.get('owner')))
        if type(candidate_family) is not str:
            return False

        review_actor = review['actor']
        if review_actor in AGENTS:
            reviewer_family = review.get('family', review_actor)
            reviewer_valid = reviewer_family == review_actor
        else:
            reviewer_family = review.get('family')
            reviewer_valid = self._session_event_matches(
                db, task['id'], 'task.reviewed', revision, review_actor,
                reviewer_family, capability='task.review')

        if actor in AGENTS:
            candidate_valid = candidate_family == actor
        else:
            # The candidate event is the latest candidate transition no later
            # than the exact reviewed revision. Its provenance is inserted in
            # the same SQLite commit as task.candidate.
            candidate_valid = self._latest_session_candidate_matches(
                db, task['id'], revision, actor, candidate_family,
                task.get('owner_session_id'))

        return (reviewer_valid and candidate_valid
                and type(reviewer_family) is str
                and reviewer_family != candidate_family)

    def _session_event_matches(self, db, task_id, action, revision, actor,
                               family, session_id=None, *, capability):
        if db is None:
            with self._connection() as connection:
                return self._session_event_matches(connection, task_id, action,
                    revision, actor, family, session_id, capability=capability)
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_session_provenance'").fetchone():
            return False
        row = db.execute(
            'SELECT a.actor,p.installation_id,p.session_id,p.family,p.capability '
            'FROM hub_audit a JOIN hub_session_provenance p ON p.audit_sequence=a.sequence '
            'WHERE a.entity_id=? AND a.action=? AND a.revision=?',
            (task_id, action, revision)).fetchone()
        if row is None or row['actor'] != actor:
            return False
        return (type(row['installation_id']) is str and actor == 'installation:' + row['installation_id']
                and (session_id is None or row['session_id'] == session_id)
                and type(row['session_id']) is str and bool(row['session_id'])
                and row['family'] == family
                and row['capability'] == capability)

    def _latest_session_candidate_matches(self, db, task_id, reviewed_revision,
                                          actor, family, session_id):
        if db is None:
            with self._connection() as connection:
                return self._latest_session_candidate_matches(connection, task_id,
                    reviewed_revision, actor, family, session_id)
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_session_provenance'").fetchone():
            return False
        row = db.execute(
            'SELECT a.revision,a.actor,p.installation_id,p.session_id,p.family,p.capability '
            'FROM hub_audit a LEFT JOIN hub_session_provenance p ON p.audit_sequence=a.sequence '
            "WHERE a.entity_id=? AND a.action='task.candidate' AND a.revision<? "
            'ORDER BY a.revision DESC LIMIT 1', (task_id, reviewed_revision)).fetchone()
        if row is None or row['actor'] != actor:
            return False
        return (type(row['installation_id']) is str and actor == 'installation:' + row['installation_id']
                and row['session_id'] == session_id
                and row['family'] == family
                and row['capability'] == 'task.candidate')

    def create_decision(self, actor, project, title, options, protected=False, *, details=None, links=None, allow_text=False,
                        client_request_id=None):
        self._actor(actor)
        title = self._text(title)
        if type(protected) is not bool or not isinstance(options, list) or not 2 <= len(options) <= 5:
            raise HubError('invalid_options')
        options = [self._text(option) for option in options]
        if len(set(options)) != len(options):
            raise HubError('invalid_options')
        if type(allow_text) is not bool or (protected and allow_text):
            raise HubError('invalid_options')
        context = self._context(details, links)
        if allow_text:
            context['allow_text'] = True
        key = self._text(client_request_id) if client_request_id is not None else None
        fingerprint = hashlib.sha256(_json([title, options, protected, context]).encode()).hexdigest()
        with self._transaction() as db:
            self._project(project, db)
            if key is not None:
                # Keep unkeyed databases readable by earlier enrollment tools.
                # DDL, creation, and receipt commit in the same transaction.
                db.execute('CREATE TABLE IF NOT EXISTS hub_decision_requests ('
                           'actor TEXT NOT NULL, project TEXT NOT NULL, key TEXT NOT NULL, '
                           'fingerprint TEXT NOT NULL, receipt TEXT NOT NULL, PRIMARY KEY(actor,project,key))')
                prior = db.execute('SELECT fingerprint,receipt FROM hub_decision_requests '
                                   'WHERE actor=? AND project=? AND key=?', (actor, project, key)).fetchone()
                if prior:
                    if prior['fingerprint'] != fingerprint:
                        raise HubError('idempotency_conflict', 409)
                    return json.loads(prior['receipt'])
            item = self._save(db, actor, 'decision.created', 'decision', dict(id=str(uuid.uuid4()), project=project,
                title=title, options=options, protected=protected, status='open', answer=None,
                execution_authorization=False, **context), new=True)
            if key is not None:
                db.execute('INSERT INTO hub_decision_requests VALUES(?,?,?,?,?)',
                           (actor, project, key, fingerprint, _json(item)))
            return item

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
                if item['status'] == 'withdrawn':
                    raise HubError('withdrawn', 409)
                # A queued manifest/review may already bind the task revision.
                # Do not mutate that admission surface through this new API.
                if self.queued_pilot_enrolled(db):
                    raise HubError('queued_pilot_admission_required', 409)
                self._no_active_run(db, identity)
                if self._open_work_job(db, identity):
                    raise HubError('active_work_job', 409)
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

    def get_decision(self, identity):
        return self._read(identity, 'decision')

    @staticmethod
    def _open_work_job(db, task_id, *, excluding_job_id=None):
        """A queued, claimed or held work-queue job still owns this task; terminal ones don't."""
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_work_jobs'").fetchone():
            return False
        query = '''SELECT 1 FROM hub_work_jobs WHERE json_extract(target,'$.task_id')=?
            AND state NOT IN ('succeeded','failed','cancelled')'''
        parameters = [task_id]
        if excluding_job_id is not None:
            query += ' AND id<>?'
            parameters.append(excluding_job_id)
        query += ' LIMIT 1'
        return db.execute(query, parameters).fetchone() is not None

    @staticmethod
    def _creator(db, kind, identity):
        marks = ','.join('?' * len(CREATION_ACTIONS[kind]))
        row = db.execute(f'SELECT actor FROM hub_audit WHERE entity_id=? AND revision=1 AND action IN ({marks}) '
                         'ORDER BY sequence LIMIT 1', (identity, *CREATION_ACTIONS[kind])).fetchone()
        return row['actor'] if row else None

    def withdraw(self, actor, kind, identity, expected_revision, reason, superseded_by=None):
        """Retire an unstarted task or an unanswered, unprotected decision. Nothing is deleted.

        A human may withdraw any eligible item; an agent only one it created, per the
        audit log. Withdrawal never approves, answers or reviews anything: a task with a
        candidate, review or active run, a protected decision and an answered decision
        are refused. Retrying the same withdrawal returns the withdrawn item.
        """
        self._actor(actor)
        if kind not in CREATION_ACTIONS:
            raise HubError('invalid_kind')
        try:
            reason = bounded_text(reason, 500)
        except ValueError:
            raise HubError('invalid_reason') from None
        if superseded_by is not None and (type(superseded_by) is not str or _UUID.fullmatch(superseded_by) is None
                                          or superseded_by == identity):
            raise HubError('invalid_replacement')
        with self._transaction() as db:
            item = self._get(db, identity, kind)
            def authorized_replacement():
                # Check scope before inspecting replacement state.  Both the
                # original request and an idempotent retry must keep foreign
                # entity existence and status private from session agents.
                try:
                    replacement = self._get(db, superseded_by, kind)
                    if session_context.actor_allowed(actor):
                        self._project(replacement['project'], db)
                except HubError as error:
                    if (session_context.actor_allowed(actor) and error.code in
                            {'not_found', 'hub_project_forbidden', 'project_archived'}):
                        raise HubError('hub_replacement_unavailable', 404) from None
                    raise
                return replacement
            # Archived projects keep their records; retiring a stale one there is allowed.
            self._project(item['project'], db, require_active=False)
            if (actor in AGENTS or session_context.actor_allowed(actor)) and self._creator(db, kind, identity) != actor:
                raise HubError('forbidden', 403)
            if (session_context.actor_allowed(actor) and item.get('owner') == actor
                    and not session_context.continuation_allowed(item, actor)):
                raise HubError('forbidden', 403)
            if item['status'] == 'withdrawn':
                if (item.get('withdrawn_by') == actor and item.get('withdrawn_reason') == reason
                        and item.get('superseded_by') == superseded_by):
                    if session_context.actor_allowed(actor) and superseded_by is not None:
                        authorized_replacement()
                    return item
                raise HubError('withdrawn', 409)
            self._cas(item, expected_revision)
            if kind == 'decision':
                if item.get('protected') is not False:
                    raise HubError('protected_decision', 409)
                if item['status'] != 'open':
                    raise HubError('decision_closed', 409)
            else:
                if (item['status'] not in WITHDRAWABLE_TASK_STATUSES or item.get('candidate_commit') is not None
                        or item.get('review') is not None):
                    raise HubError('task_in_progress', 409)
                self._no_active_run(db, identity)
                # Accepted queue work keeps its reservation; withdrawal never cancels or releases it.
                if self._open_work_job(db, identity):
                    raise HubError('task_in_progress', 409)
            if superseded_by is not None:
                replacement = authorized_replacement()
                if replacement['status'] == 'withdrawn':
                    raise HubError('invalid_replacement')
                if not session_context.actor_allowed(actor):
                    self._project(replacement['project'], db)
            if session_context.actor_allowed(actor):
                principal = session_context.current.get().principal
                item.update(withdrawn_family=principal.family, withdrawn_session_id=principal.session_id)
            item.update(status='withdrawn', withdrawn_reason=reason, superseded_by=superseded_by,
                        withdrawn_by=actor, withdrawn_at=datetime.now(timezone.utc).isoformat())
            return self._save(db, actor, kind + '.withdrawn', kind, item)

    def answer_decision(self, actor, identity, expected_revision, answer):
        self._actor(actor, human=True)
        with self._transaction() as db:
            item = self._get(db, identity, 'decision')
            self._project(item['project'], db)
            self._cas(item, expected_revision)
            # Recheck persisted records too: older releases could save conflicting
            # flags. Only an explicitly unprotected decision permits free text.
            free_text = item.get('allow_text') is True and item.get('protected') is False
            if free_text:
                try:
                    answer = bounded_text(answer, 4096)
                except ValueError:
                    raise HubError('invalid_answer', 409) from None
            if item['status'] != 'open' or (not free_text and answer not in item['options']):
                raise HubError('invalid_answer', 409)
            item.update(answer=answer, answered_by=actor, status='answered')
            return self._save(db, actor, 'decision.answered', 'decision', item)

    def create_run(self, actor, task_id, expected_revision, artifact_digest, target, *, execution_manifest_digest=None, client_request_id=None):
        if actor not in AGENTS:
            raise HubError('forbidden', 403)
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
            authorized_job_id = self._assert_queued_pilot('create_run', db=db, actor=actor,
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
            if self._open_work_job(db, task_id, excluding_job_id=authorized_job_id):
                raise HubError('active_work_job', 409)
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
