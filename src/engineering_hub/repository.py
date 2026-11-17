"""Local, transactional Hub state. This module never dispatches work."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
import uuid

from agent_message_bus import session_hub_context as session_context
from .pilot_ownership import PilotOwner
from .image_state_boundary import ImageStateBoundaryError, require_image_state_mount
from .entity_context import MAX_LINKS, bounded_text, validated_context


PROJECTS = frozenset({'engineering-hub', '3d-viewer', 'project-alpha', 'ledge-top-ops', 'treedar'})
AGENTS = frozenset({'hermes', 'codex', 'claude'})
# Audit actions that set a task's status. Run bookkeeping saves the task without
# changing it, so those actions must not reset how long a task has been waiting.
TASK_STATUS_ACTIONS = ('task.created', 'task.proposed', 'task.claimed', 'task.claim_recovered', 'task.candidate', 'task.reviewed',
                       'task.context_invalidated', 'task.withdrawn', 'task.reassigned', 'task.waiting',
                       'task.waiting_resolved', 'task.waiting_retracted', 'task.done')
# hub_task_update may set only these; claim/candidate/review keep their own flows.
UPDATE_STATUSES = frozenset({'waiting_on_beau', 'done', 'assigned', 'claimed', 'in_review', 'verified'})
MAX_ACCEPTANCE, MAX_NOTES = 20, 50
TASK_UPDATE_RECEIPT = 'task_update_receipt'
# Long backlog text left out of hub_overview; hub_task_get returns the full record.
OVERVIEW_OMITTED = frozenset({'acceptance', 'notes', 'request_text', 'completion_evidence', 'waiting_resolved',
                              'waiting_retracted'})
# A question to Beau: exactly three agent-written options, one recommended, plus his own text.
WAITING_OPTIONS, WAITING_LEVELS, MAX_BULK_RESOLVE = 3, frozenset({'now', 'later'}), 50
WAITING_FIELDS = frozenset({'type', 'text', 'options', 'recommended', 'recommended_reason', 'level'})
# Audit actions that record who created a task or decision. Withdrawal by an agent
# is limited to the agent named here, read from the audit log, never from the record.
CREATION_ACTIONS = {'task': ('task.created', 'task.proposed'), 'decision': ('decision.created',)}
WITHDRAWABLE_TASK_STATUSES = frozenset({'assigned', 'claimed'})
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
HELD_REASONS = frozenset({'candidate_outcome_uncertain', 'staging_paused', 'rollback_failed',
                         'rollback_outcome_uncertain', 'rollback_smoke_failed', 'rollback_smoke_outcome_uncertain'})


class HubError(Exception):
    """A fixed error code; field and limit name the input that failed and its bound, never its value."""

    def __init__(self, code, status=400, *, field=None, limit=None, index=None, task_id=None):
        self.code, self.status, self.field, self.limit = code, status, field, limit
        self.index, self.task_id = index, task_id
        super().__init__(self.message)

    @property
    def message(self):
        text = self.code if self.field is None else f'{self.code}: {self.field}' + (
            '' if self.limit is None else f' (limit {self.limit})')
        return text if self.index is None else f'{text} [item {self.index}, task {self.task_id}]'

    @property
    def detail(self):
        extra = {key: value for key, value in (('field', self.field), ('limit', self.limit), ('index', self.index),
                                               ('task_id', self.task_id)) if value is not None}
        return dict(code=self.code, **extra)


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
        if type(links) is list and len(links) > MAX_LINKS:
            raise HubError('invalid_context', field='links', limit=MAX_LINKS)
        try:
            return validated_context(details, links)
        except ValueError:
            raise HubError('invalid_context') from None

    @staticmethod
    def _bounded(value, maximum, code='invalid_input', field=None):
        try:
            return bounded_text(value, maximum)
        except ValueError:
            raise HubError(code, field=field, limit=None if field is None else maximum) from None

    def _plain(self, value, maximum, code, field, lines=False):
        """Bounded text Beau reads as written: no control, format (bidi, zero-width) or line-separator
        characters (a typed answer may keep newlines and tabs), and at least one visible character."""
        value = self._bounded(value, maximum, code, field)
        for char in value:
            if char in '\n\t' and lines:
                continue
            if unicodedata.category(char) in {'Cc', 'Cf', 'Zl', 'Zp', 'Cs', 'Co', 'Cn'}:
                raise HubError(code, field=field)
        if not any(not char.isspace() for char in value):
            raise HubError(code, field=field)
        return value

    def _backlog_fields(self, acceptance=None, owner=None, reviewer=None, request_text=None):
        """Optional backlog metadata. Only supplied fields are returned, so legacy shapes stay unchanged."""
        fields = {}
        if acceptance is not None:
            if type(acceptance) is not list or len(acceptance) > MAX_ACCEPTANCE:
                raise HubError('invalid_acceptance')
            fields['acceptance'] = [self._bounded(item, 300, 'invalid_acceptance') for item in acceptance]
        for name, value in (('owner', owner), ('reviewer', reviewer)):
            if value is not None:
                if value not in AGENTS:
                    raise HubError('invalid_' + name)
                fields[name] = value
        if owner is not None and owner == reviewer:
            raise HubError('independent_review_required', 403)
        if request_text is not None:
            fields['request_text'] = self._bounded(request_text, 2000)
        return fields

    def create_task(self, actor, project, title, client_request_id, *, details=None, links=None,
                    acceptance=None, owner=None, reviewer=None, request_text=None):
        """Humans and agents may add backlog tasks. Neither grants environment or production rights:
        an agent-created task records proposed_by without a scope, which task_allows_environment vetoes."""
        self._actor(actor)
        human = actor not in AGENTS and not session_context.actor_allowed(actor)
        title, key = self._text(title), self._text(client_request_id)
        context = self._context(details, links)
        backlog =self._backlog_fields(acceptance, owner, reviewer, request_text)
        # Keep the legacy human fingerprint byte-for-byte when no backlog field is supplied.
        payload = ([] if human else ['agent_task']) + [project, title] + ([context] if context else []) + (
            [backlog] if backlog else [])
        fingerprint = hashlib.sha256(_json(payload).encode()).hexdigest()
        with self._transaction() as db:
            self._project(project, db)
            prior = db.execute('SELECT * FROM hub_requests WHERE actor=? AND key=?', (actor, key)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise HubError('idempotency_conflict', 409)
                return self._get(db, prior['entity_id'], 'task')
            provenance = {} if human else dict(proposed_by=actor, **(
                {'proposed_family': session_context.family(actor)} if session_context.actor_allowed(actor) else {}))
            item = dict(id=str(uuid.uuid4()), project=project, title=title, status='assigned', owner=None,
                        candidate_commit=None, review=None, repair_count=0, **provenance, **context)
            item.update(backlog)
            item = self._save(db, actor, 'task.created', 'task', item, new=True)
            db.execute('INSERT INTO hub_requests VALUES(?,?,?,?)', (actor, key, fingerprint, item['id']))
            return item

    def _is_owner(self, item, actor):
        owner = item.get('owner')
        if owner is None:
            return False
        if owner in AGENTS and session_context.family(actor) == owner:
            return True
        return owner == actor and session_context.continuation_allowed(item, actor)

    def _is_reviewer(self, item, actor):
        return item.get('reviewer') in AGENTS and session_context.family(actor) == item['reviewer']

    @staticmethod
    def _owner_family(item):
        return item.get('owner_family') or item.get('owner')

    @staticmethod
    def _is_creator(item, actor):
        """The agent family that created the task, from its server-set provenance; humans' tasks have none."""
        creator = item.get('proposed_by')
        if type(creator) is not str:
            return False
        family = item.get('proposed_family') if creator.startswith('installation:') else creator
        return family in AGENTS and session_context.family(actor) == family

    def update_task(self, actor, identity, expected_revision, *, details=None, links=None, acceptance=None,
                    owner=None, reviewer=None, note=None, status=None, waiting=None, evidence=None,
                    client_request_id=None, title=None, retract_reason=None):
        """CAS edit by the owner, the named reviewer or a human. Omitted fields are unchanged.

        Status here covers only the backlog states: waiting_on_beau (with a decision/action item),
        done (human from any open state, the owner only after verified, with evidence) and, for a
        human, leaving waiting_on_beau back to the previous status. Claim, candidate and review keep
        their own flows. Nothing here approves, reviews or grants environment access.

        An agent's decision item carries three options, one recommended with a reason. Only Beau
        answers or dismisses an item; retract_reason lets the family that raised it, or the task's
        owner, take it back when it was settled another way or is no longer needed.

        Content (details, links, acceptance) and the title are editable by a human, or by an agent
        only on a task its own family created. Owner and reviewer are set by a human, or by the creating
        agent while the task is still assigned. With client_request_id, an identical retry returns the
        original result and a changed payload under the same id conflicts.
        """
        self._actor(actor)
        key = None if client_request_id is None else self._text(client_request_id, 128)
        human = actor not in AGENTS and not session_context.actor_allowed(actor)
        backlog = self._backlog_fields(acceptance, owner, reviewer)
        content = self._context(details, links)
        if 'acceptance' in backlog:
            content['acceptance'] = backlog.pop('acceptance')
        note = None if note is None else self._bounded(note, 500, field='note')
        title = None if title is None else self._bounded(title, 200, field='title')
        retract = None if retract_reason is None else self._plain(retract_reason, 500, 'invalid_input', 'retract_reason')
        if status is not None and status not in UPDATE_STATUSES:
            raise HubError('invalid_status')
        if (status == 'waiting_on_beau') != (waiting is not None):
            raise HubError('invalid_waiting_item')
        # A request that sent no level keeps its pre-level fingerprint, so earlier receipts still replay.
        sent_level = isinstance(waiting, dict) and waiting.get('level') is not None
        if waiting is not None:
            waiting = self._waiting_item(waiting, human)
        if retract is not None and (status or content or backlog or title or evidence is not None):
            raise HubError('invalid_input', field='retract_reason')
        if evidence is not None:
            evidence = self._bounded(evidence, 2000, 'invalid_evidence', field='evidence')
        if not (content or backlog or note or status or title or retract):
            raise HubError('invalid_input')
        # New fields join the fingerprint only when supplied, so earlier receipts still replay.
        extra = [[name, value] for name, value in (('title', title), ('retract', retract)) if value is not None]
        printed = waiting if waiting is None or sent_level else {k: v for k, v in waiting.items() if k != 'level'}
        fingerprint = hashlib.sha256(_json([identity, expected_revision, content, backlog, note, status,
                                            printed, evidence] + extra).encode()).hexdigest()
        # Receipts reuse hub_requests (namespaced key) and an inert receipt row holding the original
        # result, so the ledger keeps its existing table set.
        receipt_key = None if key is None else 'task_update:' + key
        with self._transaction() as db:
            if receipt_key is not None:
                prior = db.execute('SELECT * FROM hub_requests WHERE actor=? AND key=?', (actor, receipt_key)).fetchone()
                if prior:
                    if prior['fingerprint'] != fingerprint:
                        raise HubError('idempotency_conflict', 409)
                    return self._get(db, prior['entity_id'], TASK_UPDATE_RECEIPT)['result']
            result = self._update_task(db, actor, human, identity, expected_revision, content, backlog, note,
                                       status, waiting, evidence, title, retract)
            if receipt_key is not None:
                receipt = str(uuid.uuid4())
                db.execute('INSERT INTO hub_entities(id,kind,data) VALUES(?,?,?)',
                           (receipt, TASK_UPDATE_RECEIPT, _json(dict(id=receipt, task_id=identity, result=result))))
                db.execute('INSERT INTO hub_requests VALUES(?,?,?,?)', (actor, receipt_key, fingerprint, receipt))
            return result

    def _waiting_item(self, waiting, human):
        """Normalize a waiting item. An agent's decision needs exactly three options and one recommendation."""
        if (type(waiting) is not dict or not {'type', 'text'} <= set(waiting) or set(waiting) - WAITING_FIELDS
                or waiting['type'] not in {'decision', 'action'}):
            raise HubError('invalid_waiting_item')
        item = dict(type=waiting['type'], text=self._plain(waiting['text'], 300, 'invalid_waiting_item', 'waiting.text'),
                    level=waiting.get('level') or 'now')
        if item['level'] not in WAITING_LEVELS:
            raise HubError('invalid_waiting_item', field='waiting.level')
        options = waiting.get('options')
        if options is None:
            if waiting.get('recommended') is not None or waiting.get('recommended_reason') is not None:
                raise HubError('invalid_waiting_item', field='waiting.options')
            if item['type'] == 'decision' and not human:
                raise HubError('waiting_options_required', field='waiting.options', limit=WAITING_OPTIONS)
            return item
        if item['type'] != 'decision':
            raise HubError('invalid_waiting_item', field='waiting.options')
        if type(options) is not list or len(options) != WAITING_OPTIONS:
            raise HubError('invalid_waiting_item', field='waiting.options', limit=WAITING_OPTIONS)
        normalized = []
        for option in options:
            if type(option) is not dict or 'label' not in option or set(option) - {'label', 'detail'}:
                raise HubError('invalid_waiting_item', field='waiting.options')
            entry = dict(label=self._plain(option['label'], 80, 'invalid_waiting_item', 'waiting.options.label'))
            if option.get('detail') is not None:
                entry['detail'] = self._plain(option['detail'], 160, 'invalid_waiting_item', 'waiting.options.detail')
            normalized.append(entry)
        if len({entry['label'].casefold() for entry in normalized}) != WAITING_OPTIONS:
            raise HubError('invalid_waiting_item', field='waiting.options.label')
        recommended = waiting.get('recommended')
        if type(recommended) is not int or not 0 <= recommended < WAITING_OPTIONS:
            raise HubError('invalid_waiting_item', field='waiting.recommended')
        reason = self._plain(waiting.get('recommended_reason'), 160, 'invalid_waiting_item', 'waiting.recommended_reason')
        return dict(item, options=normalized, recommended=recommended, recommended_reason=reason)

    @staticmethod
    def _raiser_family(waiting_on):
        """The agent family that raised an item (by_family, or a bare family id on older rows); None when Beau did."""
        return (waiting_on or {}).get('by_family') or (
            (waiting_on or {}).get('by') if (waiting_on or {}).get('by') in AGENTS else None)

    @staticmethod
    def _question(waiting_on):
        """The exact question an outcome belongs to: asked_at identifies it; options and level as asked."""
        return dict(type=waiting_on['type'], text=waiting_on['text'], asked_at=waiting_on['at'],
                    **{key: waiting_on[key] for key in ('level', 'options', 'recommended', 'recommended_reason')
                       if waiting_on.get(key) is not None})

    def _update_task(self, db, actor, human, identity, expected_revision, content, backlog, note, status,
                     waiting, evidence, title=None, retract=None):
        item = self._get(db, identity, 'task')
        self._project(item['project'], db)
        self._cas(item, expected_revision)
        # Only an item an agent raised can be retracted: by that family, or by the task's owner. Beau's own stay his.
        raiser = self._raiser_family(item.get('waiting_on'))
        retracting = (retract is not None and not human and raiser is not None
                      and (self._is_owner(item, actor) or session_context.family(actor) == raiser))
        if not (human or retracting or self._is_owner(item, actor) or self._is_reviewer(item, actor)
                or self._is_creator(item, actor)):
            raise HubError('forbidden', 403)
        current = item['status']
        if current in {'withdrawn', 'done'}:
            raise HubError('task_closed', 409)
        now = datetime.now(timezone.utc).isoformat()
        if retract is not None:
            if current != 'waiting_on_beau' or not item.get('waiting_on'):
                raise HubError('not_waiting', 409)
            # Beau dismisses from the Inbox; another family's item is retracted only by the task's owner.
            if not retracting:
                raise HubError('forbidden', 403)
            if note:
                item['notes'] = (item.get('notes') or [])[-(MAX_NOTES - 1):] + [dict(at=now, by=actor, text=note)]
            waiting_on = item.pop('waiting_on')
            item.pop('waiting_resolved', None)
            item.update(status=waiting_on['previous_status'], waiting_retracted=dict(
                self._question(waiting_on), by=actor, at=now, reason=retract))
            return self._save(db, actor, 'task.waiting_retracted', 'task', item)
        if current == 'waiting_on_beau' and status is not None and not human:
            raise HubError('waiting_on_beau', 409)
        action = 'task.updated'
        if title is not None:
            if not (human or self._is_creator(item, actor)):
                raise HubError('forbidden', 403)
            item['title'] = title
        if content:
            if not (human or self._is_creator(item, actor)):
                raise HubError('forbidden', 403)
            if self.queued_pilot_enrolled(db):
                raise HubError('queued_pilot_admission_required', 409)
            self._no_active_run(db, identity)
            # Changed instructions never keep an earlier candidate's review, as in update_context.
            if self._invalidate(item):
                action = 'task.context_invalidated'
            item.update(content)
        if 'owner' in backlog or 'reviewer' in backlog:
            # Only a human reassigns; the creating agent may set both while the task is unclaimed.
            if not (human or (self._is_creator(item, actor) and item['status'] == 'assigned')):
                raise HubError('forbidden', 403)
            merged_owner = backlog.get('owner', self._owner_family(item))
            if backlog.get('reviewer', item.get('reviewer')) == merged_owner:
                raise HubError('independent_review_required', 403)
            # Compare by family: re-sending the current owner's family keeps an installation's claim.
            if 'owner' in backlog and backlog['owner'] != self._owner_family(item):
                # Reassignment is only for unstarted work; the new owner claims it to start.
                if item['status'] not in WITHDRAWABLE_TASK_STATUSES or item.get('candidate_commit') or item.get('review'):
                    raise HubError('task_in_progress', 409)
                for field in ('owner_family', 'owner_session_id'):
                    item.pop(field, None)
                item.update(owner=backlog['owner'], status='assigned')
                action = 'task.reassigned'
            if 'reviewer' in backlog:
                item['reviewer'] = backlog['reviewer']
        if note:
            item['notes'] = (item.get('notes') or [])[-(MAX_NOTES - 1):] + [dict(at=now, by=actor, text=note)]
        if status == 'waiting_on_beau':
            if item['status'] == 'waiting_on_beau':
                raise HubError('waiting_on_beau', 409)
            family = {} if human else {'by_family': session_context.family(actor)}
            # A new question starts clean: earlier outcomes stay in hub_audit, not beside the new item.
            for field in ('waiting_resolved', 'waiting_retracted'):
                item.pop(field, None)
            item.update(status='waiting_on_beau', waiting_on=dict(waiting, previous_status=item['status'], by=actor,
                                                                  at=now, **family))
            action = 'task.waiting'
        elif status == 'done':
            if not human and not (self._is_owner(item, actor) and item['status'] == 'verified'):
                raise HubError('forbidden', 403)
            if not human and evidence is None:
                raise HubError('invalid_evidence')
            self._no_active_run(db, identity)
            if item['status'] == 'waiting_on_beau':
                # Beau's item is closed with the task, and that is audited, not silently dropped.
                item = self._resolve(db, actor, item, dict(note='Closed with the task as done'), now)
            item.update(status='done', completed_by=actor, completed_at=now,
                        **({'completion_evidence': evidence} if evidence is not None else {}))
            action = 'task.done'
        elif status is not None:
            waiting_on = item.get('waiting_on')
            if item['status'] != 'waiting_on_beau' or not waiting_on or status != waiting_on['previous_status']:
                raise HubError('invalid_transition', 409)
            return self._resolve(db, actor, item, {}, now)
        return self._save(db, actor, action, 'task', item)

    @staticmethod
    def _invalidate(item):
        """Changed instructions never keep an earlier candidate's review. A task waiting on Beau
        stays waiting; it returns to the reset status when he clears the item."""
        if item.get('candidate_commit') is None and item.get('review') is None:
            return False
        reset = 'claimed' if item.get('owner') else 'assigned'
        item.update(candidate_commit=None, review=None)
        if item['status'] == 'waiting_on_beau' and item.get('waiting_on'):
            item['waiting_on'] = dict(item['waiting_on'], previous_status=reset)
        else:
            item['status'] = reset
        return True

    @staticmethod
    def _resolution_record(waiting_on, resolution, strict):
        """What Beau did with an item: answered (an option or his text), dismissed, or done (legacy shape)."""
        record = {'note': resolution['note']} if resolution.get('note') else {}
        if resolution.get('dismiss'):
            return dict(record, outcome='dismissed')
        if resolution.get('option') is not None:
            options = waiting_on.get('options') or []
            if not 0 <= resolution['option'] < len(options):
                raise HubError('invalid_option', 409, field='option')
            return dict(record, outcome='answered', answer=dict(option=resolution['option'],
                                                                label=options[resolution['option']]['label']))
        if resolution.get('answer') is not None:
            if waiting_on['type'] != 'decision':
                raise HubError('invalid_answer', 409, field='answer')
            return dict(record, outcome='answered', answer=dict(text=resolution['answer']))
        if waiting_on.get('options'):
            # Beau answers a question with options, or dismisses it; Done alone is for actions. Clearing it
            # another way (status back, or the task done) records a dismissal, so every question has an outcome.
            if strict:
                raise HubError('answer_required', 409)
            return dict(record, outcome='dismissed')
        return record

    def _resolve(self, db, actor, item, resolution, now, strict=False):
        record = self._resolution_record(item['waiting_on'], resolution, strict)
        waiting_on = item.pop('waiting_on')
        item.pop('waiting_retracted', None)
        item.update(status=waiting_on['previous_status'], waiting_resolved=dict(
            self._question(waiting_on), by=actor, at=now, **record))
        return self._save(db, actor, 'task.waiting_resolved', 'task', item)

    def _resolution(self, note=None, option=None, answer=None, dismiss=False):
        resolution = {}
        if note is not None and not (type(note) is str and not note.strip()):
            resolution['note'] = self._plain(note, 2000, 'invalid_input', 'note', lines=True)
        if option is not None:
            if type(option) is not int or not 0 <= option < WAITING_OPTIONS:
                raise HubError('invalid_option', field='option', limit=WAITING_OPTIONS - 1)
            resolution['option'] = option
        if answer is not None:
            resolution['answer'] = self._plain(answer, 2000, 'invalid_input', 'answer', lines=True)
        if dismiss is True:
            resolution['dismiss'] = True
        elif dismiss is not False:
            raise HubError('invalid_input', field='dismiss')
        if sum(key in resolution for key in ('option', 'answer', 'dismiss')) > 1:
            raise HubError('invalid_input', field='option')
        return resolution

    def _resolve_one(self, db, actor, identity, expected_revision, resolution):
        item = self._get(db, identity, 'task')
        self._project(item['project'], db)
        resolved = item.get('waiting_resolved')
        if (item['status'] != 'waiting_on_beau' and isinstance(resolved, dict) and resolved.get('by') == actor
                and item['revision'] == expected_revision + 1
                and {key: resolved.get(key) for key in ('note', 'outcome', 'answer')} == self._replay_view(resolved, resolution)):
            return item
        self._cas(item, expected_revision)
        if item['status'] != 'waiting_on_beau' or not item.get('waiting_on'):
            raise HubError('not_waiting', 409)
        return self._resolve(db, actor, item, resolution, datetime.now(timezone.utc).isoformat(), strict=True)

    @staticmethod
    def _replay_view(resolved, resolution):
        """The note/outcome/answer this resolution would have recorded, for an idempotent retry."""
        outcome = 'dismissed' if resolution.get('dismiss') else (
            'answered' if 'option' in resolution or 'answer' in resolution else None)
        answer = None
        if 'option' in resolution:
            answer = dict(option=resolution['option'], label=(resolved.get('answer') or {}).get('label'))
        elif 'answer' in resolution:
            answer = dict(text=resolution['answer'])
        return dict(note=resolution.get('note'), outcome=outcome, answer=answer)

    def resolve_waiting(self, actor, identity, expected_revision, note=None, *, option=None, answer=None,
                        dismiss=False):
        """Human-only: answer (an option or text), dismiss, or mark done Beau's item; the task returns to
        its previous status. Retrying the same resolution returns the task unchanged. Never approval."""
        self._actor(actor, human=True)
        resolution = self._resolution(note, option, answer, dismiss)
        with self._transaction() as db:
            return self._resolve_one(db, actor, identity, expected_revision, resolution)

    def resolve_waiting_many(self, actor, items):
        """Human-only: answer or dismiss several items in one transaction; all apply or none do."""
        self._actor(actor, human=True)
        if type(items) is not list or not 1 <= len(items) <= MAX_BULK_RESOLVE:
            raise HubError('invalid_input', field='items', limit=MAX_BULK_RESOLVE)
        def located(index, entry, step):
            try:
                return step()
            except HubError as error:
                task_id = entry.get('task_id') if isinstance(entry, dict) and type(entry.get('task_id')) is str else None
                raise HubError(error.code, error.status, field=error.field, limit=error.limit, index=index,
                               task_id=task_id) from None

        def parsed(entry):
            if type(entry) is not dict or set(entry) - {'task_id', 'expected_revision', 'note', 'option', 'answer', 'dismiss'}:
                raise HubError('invalid_input', field='items')
            return self._resolution(entry.get('note'), entry.get('option'), entry.get('answer'), entry.get('dismiss', False))

        resolutions = [located(index, entry, lambda entry=entry: parsed(entry)) for index, entry in enumerate(items)]
        seen = set()
        for index, entry in enumerate(items):
            if entry.get('task_id') in seen:
                raise HubError('invalid_input', field='task_id', index=index, task_id=entry.get('task_id'))
            seen.add(entry.get('task_id'))
        with self._transaction() as db:
            return [located(index, entry, lambda entry=entry, resolution=resolution: self._resolve_one(
                db, actor, entry.get('task_id'), entry.get('expected_revision'), resolution))
                for index, (entry, resolution) in enumerate(zip(items, resolutions))]

    def set_waiting_level(self, actor, identity, expected_revision, level):
        """Human-only: move an open item between now and later. Setting the current level is a no-op."""
        self._actor(actor, human=True)
        if level not in WAITING_LEVELS:
            raise HubError('invalid_input', field='level')
        with self._transaction() as db:
            item = self._get(db, identity, 'task')
            self._project(item['project'], db)
            if item['status'] != 'waiting_on_beau' or not item.get('waiting_on'):
                raise HubError('not_waiting', 409)
            if (item['waiting_on'].get('level') or 'now') == level:
                return item
            self._cas(item, expected_revision)
            item['waiting_on'] = dict(item['waiting_on'], level=level)
            return self._save(db, actor, 'task.waiting_level', 'task', item)

    @staticmethod
    def overview_task(task):
        """Compact task view for hub_overview: long backlog text stays in hub_task_get."""
        compact = {key: value for key, value in task.items() if key not in OVERVIEW_OMITTED}
        if task.get('acceptance'):
            compact['acceptance_count'] = len(task['acceptance'])
        if task.get('notes'):
            compact['notes_count'] = len(task['notes'])
        return compact

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
            return True
        task = json.loads(row['data'])
        if task.get('status') in {'withdrawn', 'done'}:
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
            # A backlog task assigned to a named agent can be claimed only by that agent family.
            if item.get('owner') is not None and session_context.family(actor) != item['owner']:
                raise HubError('forbidden', 403)
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
            if item['status'] in {'done', 'waiting_on_beau'}:
                raise HubError('task_closed' if item['status'] == 'done' else 'waiting_on_beau', 409)
            self._no_active_run(db, identity)
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
            if (item['owner'] == actor
                    or session_context.family(actor) == item.get('candidate_family', item.get('owner_family', item['owner']))):
                raise HubError('independent_review_required', 403)
            # A named reviewer binds who may review; tasks without one keep the open rule.
            if item.get('reviewer') is not None and session_context.family(actor) != item['reviewer']:
                raise HubError('named_reviewer_required', 403)
            if item['status'] != 'in_review' or not commit or item['candidate_commit'] != commit:
                raise HubError('stale_review', 409)
            item.update(status='verified' if verdict == 'approve' else 'claimed',
                        review=dict(actor=actor, commit=commit, verdict=verdict,
                            **({'family': session_context.family(actor)} if session_context.actor_allowed(actor) else {})) if verdict == 'approve' else None)
            return self._save(db, actor, 'task.reviewed', 'task', item)

    def create_decision(self, actor, project, title, options, protected=False, *, details=None, links=None, allow_text=False):
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
                if item['status'] == 'withdrawn':
                    raise HubError('withdrawn', 409)
                # A queued manifest/review may already bind the task revision.
                # Do not mutate that admission surface through this new API.
                if self.queued_pilot_enrolled(db):
                    raise HubError('queued_pilot_admission_required', 409)
                self._no_active_run(db, identity)
                if item['status'] == 'done':
                    raise HubError('task_closed', 409)
                if self._invalidate(item):
                    action = 'task.context_invalidated'
            for field in ('details', 'links'):
                item.pop(field, None)
            item.update(context)
            return self._save(db, actor, action, kind, item)

    def list_decisions(self):
        return self._list('decision')

    @staticmethod
    def _open_work_job(db, task_id):
        """A queued, claimed or held work-queue job still owns this task; terminal ones don't."""
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_work_jobs'").fetchone():
            return False
        return db.execute('''SELECT 1 FROM hub_work_jobs WHERE json_extract(target,'$.task_id')=?
            AND state NOT IN ('succeeded','failed','cancelled') LIMIT 1''', (task_id,)).fetchone() is not None

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
            # Archived projects keep their records; retiring a stale one there is allowed.
            self._project(item['project'], db, require_active=False)
            agent = actor in AGENTS or session_context.actor_allowed(actor)
            if agent and self._creator(db, kind, identity) != actor:
                raise HubError('forbidden', 403)
            # A creating agent may withdraw its task only while nobody has claimed it.
            if (agent and kind == 'task' and item['status'] != 'withdrawn'
                    and (item['status'] != 'assigned' or item.get('owner_session_id') is not None)):
                raise HubError('forbidden', 403)
            if (session_context.actor_allowed(actor) and item.get('owner') == actor
                    and not session_context.continuation_allowed(item, actor)):
                raise HubError('forbidden', 403)
            if item['status'] == 'withdrawn':
                if (item.get('withdrawn_by') == actor and item.get('withdrawn_reason') == reason
                        and item.get('superseded_by') == superseded_by):
                    if session_context.actor_allowed(actor) and superseded_by is not None:
                        replacement = self._get(db, superseded_by, kind)
                        self._project(replacement['project'], db)
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
                replacement = self._get(db, superseded_by, kind)
                if replacement['status'] == 'withdrawn':
                    raise HubError('invalid_replacement')
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
