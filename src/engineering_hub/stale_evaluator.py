"""Explicitly opted-in advisory evaluator. No delivery or execution authority."""
import hashlib
import json
import math
import threading
import uuid
from dataclasses import dataclass, field

from agent_message_bus import session_hub_context as context
from agent_message_bus.agent_sessions import AgentSessionError

from .dispatch import DispatchRepository
from .repository import HubError
from .roles import RoleRepository, _identity, _NAMESPACE as _ROLE_NAMESPACE
from .task_progress import role_activity_id
from .session_hub_bridge import SessionHubBridge
from .stale_service_authority import StaleServiceGrant

_CONFIG = str(uuid.UUID('ee5e2654-d8da-4f7a-9444-148a55bd0344'))
_ANCHOR_NAMESPACE = uuid.UUID('7c9c0c53-5a12-4715-953d-5a9dcf176153')
_ACTOR = 'service:stale-evaluator'
_TOLERANCE = 2.0


@dataclass
class _TargetOperation:
    bridge: object
    db: object
    commit_checks: list = field(default_factory=list)


class StaleEvaluator:
    """One process-bound worker. An absent or outdated opt-in does no work."""
    def __init__(self, grant, bridge, *, batch=16, interval=10):
        if type(grant) is not StaleServiceGrant or type(bridge) is not SessionHubBridge or bridge.repo is not grant.repo:
            raise ValueError('exact_grant_and_bridge_required')
        if type(batch) is not int or not 1 <= batch <= 64 or type(interval) not in (int, float) or not 1 <= interval <= 60:
            raise ValueError('invalid_evaluator_bounds')
        self.grant, self.bridge, self.repo = grant, bridge, grant.repo
        self.authority = grant.bind()
        self._instance = str(uuid.uuid4())
        # A newly constructed service cannot inherit old approval, even if a
        # previous app instance faulted while holding the same PilotOwner.
        with self.repo._connection() as db:
            existing = grant._read(db)
            self._minimum_grant_revision = existing['revision'] if existing else 0
        self.batch, self.interval = batch, interval
        self._stop = threading.Event()
        self._thread = None
        self._thread_lock = threading.Lock()
        self._cursors = {}
        self._loss_seen = {}
        self._project_index = 0
        self.last_error = None
        self._faulted_grant_revision = None

    @staticmethod
    def _read_config(db):
        row = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='stale_evaluator_config'", (_CONFIG,)).fetchone()
        if row is None:
            return None
        try:
            item = json.loads(row['data'])
            if (type(item) is not dict or item.get('id') != _CONFIG or type(item.get('revision')) is not int
                    or item['revision'] < 1 or type(item.get('enabled')) is not bool
                    or type(item.get('grant_revision')) is not int or item['grant_revision'] < 1
                    or type(item.get('instance')) is not str):
                raise ValueError()
            return item
        except (TypeError, ValueError):
            raise HubError('stale_evaluator_config_corrupt', 409) from None

    def configure(self, actor, expected_revision, grant_revision, enabled):
        self.repo._actor(actor, human=True)
        if type(expected_revision) is not int or expected_revision < 0 or type(grant_revision) is not int or type(enabled) is not bool:
            raise HubError('invalid_stale_evaluator_config', 422)
        with self.repo._transaction() as db:
            current = self._read_config(db)
            if expected_revision != (current['revision'] if current else 0):
                raise HubError('revision_conflict', 409)
            grant = self.grant._read(db)
            if grant is None or grant['revision'] != grant_revision:
                raise HubError('stale_service_grant_inactive', 403)
            if enabled:
                if self._faulted_grant_revision == grant_revision or grant_revision <= self._minimum_grant_revision:
                    raise HubError('stale_evaluator_reapproval_required', 409)
                for project in grant['projects']:
                    self._admission_check(db, project, grant)
            item = dict(id=_CONFIG, revision=expected_revision, enabled=enabled, grant_revision=grant_revision, instance=self._instance)
            item = self.repo._save(db, actor, 'stale_evaluator.configured', 'stale_evaluator_config', item,
                                   new=current is None, publish=False)
            if enabled:
                for project in grant['projects']:
                    self._admission_check(db, project, grant)
            return item

    def _clock_check(self, grant):
        wall, boot, session = self.grant._now(), self.grant._monotonic(), self.bridge.sessions._now()
        wall_elapsed = wall - grant['approved_at']
        boot_elapsed = boot - grant['approved_monotonic']
        if (not all(math.isfinite(v) for v in (wall_elapsed, boot_elapsed, session))
                or wall_elapsed < 0 or boot_elapsed < 0 or abs(wall_elapsed - boot_elapsed) > _TOLERANCE
                or abs(session - wall) > _TOLERANCE):
            raise HubError('stale_evaluator_clock_untrusted', 409)

    def _admission_check(self, db, project, grant):
        # Both clock probes and authority's own subsequent probes can fail.
        # Any admission failure conservatively requires a fresh grant.
        try:
            self._clock_check(grant)
            return self.authority._check(db, project, grant['revision'])
        except (HubError, AgentSessionError):
            self._faulted_grant_revision = grant['revision']
            raise

    def _config(self, db, project):
        config = self._read_config(db)
        if config is None or not config['enabled'] or config['instance'] != self._instance:
            raise HubError('stale_evaluator_disabled', 403)
        if self._faulted_grant_revision == config['grant_revision']:
            raise HubError('stale_evaluator_reapproval_required', 409)
        grant = self.grant._read(db)
        if grant is None or grant['revision'] != config['grant_revision']:
            raise HubError('stale_service_grant_inactive', 403)
        return config, self._admission_check(db, project, grant)

    def _anchor(self, db, item, task, target, grant):
        """Persist a conservative boot-bound first observation of relevant state."""
        activity = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='role_progress_activity'",
                              (role_activity_id(item['task_id'], item['role_id']),)).fetchone()
        claim_id = str(uuid.uuid5(_ROLE_NAMESPACE, 'claim:' + item['task_id']))
        claim = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='role_claim'", (claim_id,)).fetchone()
        fingerprint = hashlib.sha256(json.dumps([item, task, target, activity['data'] if activity else None,
                                                  claim['data'] if claim else None], sort_keys=True,
                                                 separators=(',', ':')).encode()).hexdigest()
        identity = str(uuid.uuid5(_ANCHOR_NAMESPACE, item['id']))
        row = db.execute('SELECT kind,data FROM hub_entities WHERE id=?', (identity,)).fetchone()
        old = None
        if row:
            if row['kind'] != 'stale_evaluator_anchor':
                raise HubError('stale_evaluator_anchor_corrupt', 409)
            try:
                old = json.loads(row['data'])
                if (type(old) is not dict or old.get('id') != identity or old.get('dispatch_id') != item['id']
                        or old.get('project') != item['project'] or type(old.get('revision')) is not int
                        or type(old.get('observed_monotonic')) not in (int, float)
                        or not math.isfinite(old['observed_monotonic'])):
                    raise ValueError()
            except (ValueError, TypeError):
                raise HubError('stale_evaluator_anchor_corrupt', 409) from None
        now = self.grant._monotonic()
        if (old is None or old.get('fingerprint') != fingerprint or old.get('boot_id') != grant['boot_id']
                or old.get('owner_epoch') != grant['owner_epoch']):
            anchor = dict(id=identity, revision=old['revision'] + 1 if old else 1, dispatch_id=item['id'],
                          project=item['project'], boot_id=grant['boot_id'], owner_epoch=grant['owner_epoch'],
                          fingerprint=fingerprint, observed_monotonic=now)
            saved = db.execute("INSERT INTO hub_entities(id,kind,data) VALUES(?,'stale_evaluator_anchor',?) "
                               "ON CONFLICT(id) DO UPDATE SET data=excluded.data WHERE kind='stale_evaluator_anchor'",
                               (identity, json.dumps(anchor, sort_keys=True, separators=(',', ':'))))
            if saved.rowcount != 1:
                raise HubError('stale_evaluator_anchor_corrupt', 409)
            return 0.0, anchor
        if now < old['observed_monotonic']:
            raise HubError('stale_evaluator_clock_untrusted', 409)
        return now - old['observed_monotonic'], old

    def evaluate(self, project, identity):
        """Evaluate one current record. Caller never supplies an actor or authority."""
        self.bridge._owned()
        with self.bridge.sessions._transaction() as session_db:
            with self.repo._transaction() as db:
                config, grant = self._config(db, project)
                dispatch = DispatchRepository(self.repo)
                item = self.repo._get(db, identity, 'role_dispatch')
                if item.get('project') != project or item.get('state') not in ('dispatched', 'working', 'blocked', 'asked'):
                    return False
                if item.get('task_kind') not in ('task', 'tracking_task'):
                    raise ValueError('invalid task kind')
                request = self.repo._get(db, item['request_id'], 'orchestration_request')
                task = self.repo._get(db, item['task_id'], item['task_kind'])
                target = self.repo._get(db, _identity(item['role_id']), 'coordination_role')
                if (request.get('project') != project or task.get('project') != project
                        or target.get('role_id') != item['role_id'] or project not in target.get('projects', [])
                        or task.get('status') in ('withdrawn', 'verified', 'completion_verified')):
                    return False
                for role_project in target['projects']:
                    try:
                        self.repo._project(role_project, db)
                    except HubError as error:
                        if role_project != project and error.code in ('project_archived', 'invalid_project'):
                            self.last_error = 'target_role_unavailable'
                            return False
                        raise
                operation = _TargetOperation(self.bridge, session_db)
                old_revision = item['revision']
                try:
                    elapsed, anchor = self._anchor(db, item, task, target, grant)
                    window = item['window_seconds']
                    if type(window) is not int or not 10 <= window <= 86400 or elapsed < 0:
                        raise HubError('stale_evaluator_clock_untrusted', 409)
                    grace = 2 * _TOLERANCE
                    binding = item.get('target_binding') or {}
                    session_row = session_db.execute('SELECT expires_at, revoked_at FROM agent_sessions WHERE id=?',
                                                     (binding.get('session_id'),)).fetchone()
                    grant_row = session_db.execute('SELECT revision, expires_at, revoked_at FROM session_hub_grants WHERE installation_id=?',
                                                   (binding.get('installation_id'),)).fetchone()
                    fingerprint = (grant['revision'], target['revision'], tuple(session_row) if session_row else None,
                                   tuple(grant_row) if grant_row else None, json.dumps(binding, sort_keys=True))
                    previous = self._loss_seen.get(identity)
                    loss_age = (self.grant._monotonic() - previous[1]
                                if previous is not None and previous[0] == fingerprint else 0.0)
                    allow_loss = elapsed >= 3600 + grace and loss_age >= grace
                    observed_loss = []
                    dispatch._evaluate_checkpoint(db, _ACTOR, item, target, operation,
                        timeout_grace=grace, allow_timeout=elapsed >= window + grace,
                        allow_target_loss=allow_loss, loss_observed=observed_loss)
                    if observed_loss:
                        if previous is None or previous[0] != fingerprint:
                            if len(self._loss_seen) >= 4096:
                                self._loss_seen.pop(next(iter(self._loss_seen)))
                            self._loss_seen[identity] = (fingerprint, self.grant._monotonic())
                    else:
                        self._loss_seen.pop(identity, None)
                    for check in operation.commit_checks:
                        check(db)
                    if item['revision'] != old_revision and item.get('attention_intent_id'):
                        intent = self.repo._get(db, item['attention_intent_id'], 'orchestration_attention')
                        if intent.get('dispatch_revision') == item['revision']:
                            intent['evaluation_evidence'] = dict(
                                source='automatic_advisory', boot_id=grant['boot_id'],
                                owner_epoch=grant['owner_epoch'], grant_revision=grant['revision'],
                                state_fingerprint=anchor['fingerprint'],
                                observation_boottime=anchor['observed_monotonic'],
                                observed_elapsed_seconds=elapsed, report_window_seconds=window,
                                clock_grace_seconds=grace, target_loss_observed_seconds=loss_age)
                            saved = db.execute("UPDATE hub_entities SET data=? WHERE id=? AND kind='orchestration_attention'",
                                               (json.dumps(intent, sort_keys=True, separators=(',', ':')), intent['id']))
                            if saved.rowcount != 1:
                                raise HubError('stale_evaluator_evidence_unavailable', 409)
                except (HubError, AgentSessionError) as error:
                    if error.code in ('session_clock_unavailable', 'dispatch_clock_untrusted', 'stale_service_clock_untrusted', 'stale_evaluator_clock_untrusted'):
                        self._faulted_grant_revision = config['grant_revision']
                    raise
                current, _ = self._config(db, project)
                if current['revision'] != config['revision']:
                    raise HubError('stale_evaluator_config_changed', 409)
                return item['revision'] != old_revision

    def scan_once(self):
        """Visit a bounded batch in one project, rotating projects and UUID cursors."""
        self.bridge._owned()
        with self.repo._connection() as db:
            config = self._read_config(db)
            grant = self.grant._read(db)
            if config is None or not config['enabled'] or config['instance'] != self._instance or grant is None or config['grant_revision'] != grant['revision']:
                return None
            projects = grant['projects']
            project = projects[self._project_index % len(projects)]
            self._project_index += 1
            self._config(db, project)
            cursor = self._cursors.get(project, '')
            rows = db.execute("SELECT id FROM hub_entities WHERE kind='role_dispatch' AND CASE WHEN json_valid(data) THEN json_extract(data,'$.project') END=? AND CASE WHEN json_valid(data) THEN json_extract(data,'$.state') END IN ('dispatched','working','blocked','asked') AND id>? ORDER BY id LIMIT ?",
                              (project, cursor, self.batch)).fetchall()
        if not rows:
            self._cursors[project] = ''
            return 0
        count = 0
        for row in rows:
            if self._stop.is_set():
                break
            self._cursors[project] = row['id']
            try:
                self.evaluate(project, row['id'])
                count += 1
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                self.last_error = 'invalid_dispatch_data:' + type(error).__name__
            except HubError as error:
                if error.code not in ('not_found', 'invalid_role'):
                    raise
                self.last_error = 'invalid_dispatch_data:' + error.code
        return count

    def status(self, actor):
        self.repo._actor(actor, human=True)
        with self.repo._connection() as db:
            config = self._read_config(db)
            grant = self.grant._read(db)
            state = 'disabled'
            if config and config['enabled']:
                state = ('reapproval_required' if config['instance'] != self._instance or self._faulted_grant_revision == config['grant_revision'] else
                         'grant_changed' if grant is None or config['grant_revision'] != grant['revision'] else
                         'running' if self._thread is not None and self._thread.is_alive() else
                         'error' if self.last_error else 'stopped')
            return dict(revision=config['revision'] if config else 0, state=state,
                        grant_revision=config['grant_revision'] if config else None,
                        last_error=self.last_error, worker_running=bool(self._thread and self._thread.is_alive()),
                        delivery='not_configured', wake_verified=False)

    def _run(self):
        token = context.current.set(None)
        try:
            while not self._stop.is_set():
                try:
                    if self.scan_once() is None:
                        return
                except Exception as error:
                    # Authority, clock, database and owner errors stop the worker.
                    # A new explicit owner configuration is required to restart it.
                    self.last_error = getattr(error, 'code', type(error).__name__)
                    return
                self._stop.wait(self.interval)
        finally:
            context.current.reset(token)

    def start(self):
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self.bridge._owned()
            self._stop.clear()
            self.last_error = None
            self._thread = threading.Thread(target=self._run, name='hub-stale-evaluator', daemon=False)
            self._thread.start()

    def stop(self):
        with self._thread_lock:
            self._stop.set()
            if self._thread is not None:
                self._thread.join()
                self._thread = None
