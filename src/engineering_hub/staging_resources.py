"""Same-ledger staging inventory and explicit, receipt-based cleanup planning.

This module never runs a job or deletes a file. The control principal is a local
composition capability (object identity), never an HTTP/MCP argument. Job
bindings must come from the authenticated registered-job controller. A cleanup
operation is dispatched once; an uncertain outcome must be reconciled using
that operation's receipt, not redispatched by making another plan.
"""
from datetime import datetime, timezone
import hashlib
import json
import re
import time
import uuid

from .repository import AGENTS, HubError, HubRepository
from .staging_action_key import staging_action_key
from .staging_source_binding import valid_source_binding


KINDS = frozenset({'workspace', 'build', 'test_run', 'preview'})
STATES = frozenset({'provisioning', 'ready', 'failed', 'expiring', 'deleting',
                    'deleted', 'delete_failed'})
ACTIONS = frozenset({'read', 'retain', 'cleanup', 'build', 'test', 'preview'})
PROTECTIONS = frozenset({'active_job', 'current_release', 'rollback_target',
                         'held_campaign', 'decision_evidence', 'latest_failure'})
RETENTION = {'workspace': 86400, 'preview': 3 * 86400,
             'build': 14 * 86400, 'test_run': 30 * 86400}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _uuid(value):
    try:
        if type(value) is str and str(uuid.UUID(value)) == value:
            return value
    except ValueError:
        pass
    raise HubError('invalid_staging_id')


def _actor(value):
    if type(value) is not str:
        raise HubError('forbidden', 403)
    HubRepository._actor(value)
    return value


def _text(value, maximum=1000):
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise HubError('invalid_staging_text')
    return value.strip()


def _commit(value):
    if type(value) is not str or re.fullmatch('[a-f0-9]{40}', value) is None:
        raise HubError('invalid_source_commit')
    return value


def _digest(value):
    if value is not None and (type(value) is not str or
            re.fullmatch('sha256:[a-f0-9]{64}', value) is None):
        raise HubError('invalid_artifact_digest')
    return value


def _iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _seconds(value):
    try:
        if type(value) is not str or len(value) > 64:
            raise ValueError
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        return stamp.timestamp()
    except (ValueError, OverflowError):
        raise HubError('invalid_staging_time') from None


class StagingResourceStore:
    def __init__(self, repo, *, admin_actors=frozenset(), control_principal=None,
                 clock=time.time):
        if type(repo) is not HubRepository or type(admin_actors) is not frozenset or not callable(clock):
            raise HubError('invalid_staging_configuration')
        for actor in admin_actors:
            _actor(actor)
            repo._actor(actor, human=True)
        self.repo, self.admin_actors = repo, admin_actors
        self._control, self.clock = control_principal, clock
        with repo._transaction() as db:
            for sql in (
                'CREATE TABLE IF NOT EXISTS hub_staging_policy (project TEXT PRIMARY KEY, data TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS hub_staging_resource_jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS hub_staging_resource_ownership (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, project TEXT NOT NULL, created_by TEXT NOT NULL, source_commit TEXT NOT NULL)',
                'CREATE INDEX IF NOT EXISTS hub_staging_resource_ownership_job ON hub_staging_resource_ownership(job_id,id)',
                "CREATE INDEX IF NOT EXISTS hub_staging_resource_job_ref ON hub_entities((CASE WHEN json_valid(data) THEN json_extract(data,'$.job_id') END),id) WHERE kind='staging_resource'",
                "CREATE INDEX IF NOT EXISTS hub_staging_resource_bad_job_ref ON hub_entities((CASE WHEN json_valid(data) THEN CASE WHEN json_type(data,'$.job_id')='text' THEN 0 ELSE 1 END ELSE 1 END),id) WHERE kind='staging_resource'",
                'CREATE TABLE IF NOT EXISTS hub_staging_cleanup_plans (id TEXT PRIMARY KEY, actor TEXT NOT NULL, data TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS hub_staging_cleanup_receipts (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, data TEXT NOT NULL)',
                'CREATE INDEX IF NOT EXISTS hub_audit_terminal_lookup ON hub_audit(entity_id,revision,action)',
            ):
                db.execute(sql)
            # IF NOT EXISTS does not check an existing index definition. A name
            # collision could silently turn the guarded bounded lookups into scans.
            for name, table, expected in (
                ('hub_staging_resource_ownership_job', 'hub_staging_resource_ownership', 'CREATE INDEX hub_staging_resource_ownership_job ON hub_staging_resource_ownership(job_id,id)'),
                ('hub_staging_resource_job_ref', 'hub_entities', "CREATE INDEX hub_staging_resource_job_ref ON hub_entities((CASE WHEN json_valid(data) THEN json_extract(data,'$.job_id') END),id) WHERE kind='staging_resource'"),
                ('hub_staging_resource_bad_job_ref', 'hub_entities', "CREATE INDEX hub_staging_resource_bad_job_ref ON hub_entities((CASE WHEN json_valid(data) THEN CASE WHEN json_type(data,'$.job_id')='text' THEN 0 ELSE 1 END ELSE 1 END),id) WHERE kind='staging_resource'"),
            ):
                actual = db.execute('SELECT type,tbl_name,sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
                if actual is None or tuple(actual) != ('index', table, expected):
                    raise HubError('staging_resource_index_invalid', 503)

    def _trusted(self, principal):
        if self._control is None or principal is not self._control:
            raise HubError('staging_control_required', 403)

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not 0 <= value < 253402300799:
            raise HubError('invalid_staging_clock')
        return value

    def _save(self, db, actor, action, item, *, table=None, new=False):
        item['revision'] = 1 if new else item['revision'] + 1
        item['updated_at'] = _iso(self._now())
        if table:
            # table is an internal constant, never a caller-controlled identifier.
            db.execute(f'INSERT INTO {table}(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                       (item['id'], _json(item)))
        else:
            db.execute('INSERT INTO hub_entities(id,kind,data) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                       (item['id'], 'staging_resource', _json(item)))
        db.execute('INSERT INTO hub_audit(actor,action,entity_id,revision,created_at) VALUES(?,?,?,?,?)',
                   (actor, action, item['id'], item['revision'], item['updated_at']))
        self.repo._emit(db, action, item)
        return item

    def _policy(self, db, project):
        row = db.execute('SELECT data FROM hub_staging_policy WHERE project=?', (project,)).fetchone()
        return json.loads(row['data']) if row else {'revision': 0, 'grants': {}}

    def _authorize(self, db, actor, project, action):
        _actor(actor)
        if type(action) is not str or action not in ACTIONS:
            raise HubError('invalid_staging_action')
        self.repo._project(project, db)
        if action not in self._policy(db, project)['grants'].get(actor, []):
            raise HubError('staging_forbidden', 403)

    def authorize(self, actor, project, action):
        with self.repo._connection() as db:
            self._authorize(db, actor, project, action)
        return True

    def set_policy(self, actor, project, expected_revision, grants, *, admission_guard=None):
        _actor(actor)
        if actor not in self.admin_actors:
            raise HubError('staging_admin_required', 403)
        if type(grants) is not dict or len(grants) > 1000:
            raise HubError('invalid_staging_grants')
        normalized = {}
        for grantee, actions in grants.items():
            _actor(grantee)
            if type(actions) not in (list, tuple, frozenset, set) or any(type(a) is not str or a not in ACTIONS for a in actions):
                raise HubError('invalid_staging_grants')
            normalized[grantee] = sorted(set(actions))
        with self.repo._transaction(before_commit=admission_guard) as db:
            self.repo._project(project, db)
            prior = self._policy(db, project)
            self.repo._cas(prior, expected_revision)
            item = {'id': project, 'project': project, 'grants': normalized,
                    'revision': prior['revision'] + 1, 'updated_at': _iso(self._now())}
            db.execute('INSERT INTO hub_staging_policy(project,data) VALUES(?,?) ON CONFLICT(project) DO UPDATE SET data=excluded.data',
                       (project, _json(item)))
            db.execute('INSERT INTO hub_audit(actor,action,entity_id,revision,created_at) VALUES(?,?,?,?,?)',
                       (actor, 'staging.policy', project, item['revision'], item['updated_at']))
            self.repo._emit(db, 'staging.policy', item)
            return item

    def get_policy(self, actor, project):
        _actor(actor)
        if actor not in self.admin_actors:
            raise HubError('staging_admin_required', 403)
        with self.repo._connection() as db:
            self.repo._project(project, db)
            return dict(self._policy(db, project), project=project)

    @staticmethod
    def _job(db, job_id):
        row = db.execute('SELECT data FROM hub_staging_resource_jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise HubError('staging_job_not_registered', 409)
        return json.loads(row['data'])

    def bind_job(self, principal, job_id, *, project, created_by, source_commit,
                 artifact_digest=None):
        """Controller-only immutable binding from its registered job record."""
        self._trusted(principal)
        _uuid(job_id)
        _actor(created_by)
        _commit(source_commit)
        _digest(artifact_digest)
        with self.repo._transaction() as db:
            self.repo._project(project, db)
            binding = {'id': job_id, 'project': project, 'created_by': created_by,
                       'source_commit': source_commit, 'artifact_digest': artifact_digest}
            prior = db.execute('SELECT data FROM hub_staging_resource_jobs WHERE id=?', (job_id,)).fetchone()
            if prior:
                prior = json.loads(prior['data'])
                if any(prior[k] != v for k, v in binding.items()):
                    raise HubError('staging_job_binding_conflict', 409)
                return prior
            binding.update(terminal=False, protected=[], created_at=_iso(self._now()))
            return self._save(db, created_by, 'staging.job_bound', binding,
                              table='hub_staging_resource_jobs', new=True)

    def register_resource(self, principal, job_id, kind, *, topic=None):
        self._trusted(principal)
        _uuid(job_id)
        if type(kind) is not str or kind not in KINDS:
            raise HubError('invalid_staging_kind')
        if topic is not None:
            topic = _text(topic, 200)
        with self.repo._transaction() as db:
            job = self._job(db, job_id)
            self.repo._project(job['project'], db)
            if job['terminal']:
                raise HubError('staging_job_terminal', 409)
            now = self._now()
            item = {k: job[k] for k in ('project', 'created_by', 'source_commit', 'artifact_digest')}
            item.update(id=str(uuid.uuid4()), kind=kind, topic=topic, job_id=job_id,
                        created_at=_iso(now), updated_at=_iso(now), last_viewed_at=None,
                        expires_at=None if kind == 'workspace' else _iso(now + RETENTION[kind]),
                        state='provisioning', size_bytes=None, preview_path=None, keep=None,
                        protected=job['protected'] + [{'reason': 'active_job', 'ref': job_id}], revision=0)
            db.execute('INSERT INTO hub_staging_resource_ownership VALUES(?,?,?,?,?)',
                       (item['id'], job_id, item['project'], item['created_by'], item['source_commit']))
            return self._save(db, job['created_by'], 'staging.resource_registered', item, new=True)

    def _resource(self, db, identity):
        _uuid(identity)
        if not db.execute('SELECT 1 FROM hub_staging_resource_ownership WHERE id=?', (identity,)).fetchone():
            raise HubError('not_found', 404)
        return self.repo._get(db, identity, 'staging_resource')

    def _owned(self, actor, item):
        if actor in AGENTS and actor != item['created_by']:
            raise HubError('staging_resource_not_owned', 403)

    def get_resource(self, actor, identity):
        with self.repo._connection() as db:
            item = self._resource(db, identity)
            self._authorize(db, actor, item['project'], 'read')
            return item

    def inspect_resource(self, principal, identity):
        """Controller-only fact reconciliation, independent of revoked grants."""
        self._trusted(principal)
        with self.repo._connection() as db:
            return self._resource(db, identity)

    def inspect_job(self, principal, job_id):
        self._trusted(principal)
        with self.repo._connection() as db:
            return self._job(db, _uuid(job_id))

    def abandon_not_created(self, principal, job_id):
        """Record absence only from the controller's durable no-effect proof.

        This removes no host files and cannot turn an uncertain dispatch into
        absence. Public callers never possess the composition capability.
        """
        self._trusted(principal)
        with self.repo._transaction() as db:
            job = self._job(db, _uuid(job_id))
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='hub_staging_submissions'").fetchone():
                raise HubError('staging_no_effect_proof_required', 409)
            row = db.execute("SELECT data FROM hub_staging_submissions WHERE json_extract(data,'$.job_id')=?", (job_id,)).fetchone()
            proof = json.loads(row[0]) if row else {}
            if (proof.get('phase') != 'verified' or proof.get('outcome') != 'failed'
                    or proof.get('dispatch_proof') not in ('never_sent', 'definitively_rejected')
                    or any(proof.get(k) != job[k] for k in ('project', 'source_commit'))
                    or proof.get('actor') != job['created_by']):
                raise HubError('staging_no_effect_proof_required', 409)
            entries = db.execute("SELECT e.data FROM hub_entities e JOIN hub_staging_resource_ownership o ON o.id=e.id WHERE o.job_id=?", (job_id,)).fetchall()
            items = [json.loads(entry[0]) for entry in entries]
            if len(items) != 4 or {i['kind'] for i in items} != KINDS or any(
                    i['state'] not in ('provisioning', 'deleted') for i in items):
                raise HubError('staging_no_effect_proof_required', 409)
            if job['terminal'] and all(item['state'] == 'deleted' for item in items):
                return job
            for item in items:
                if item['state'] == 'deleted':
                    continue
                item.update(state='deleted', size_bytes=0, protected=[], expires_at=_iso(self._now()))
                self._save(db, job['created_by'], 'staging.resource_not_created', item)
            job.update(terminal=True, protected=[])
            return self._save(db, job['created_by'], 'staging.job_not_dispatched', job,
                              table='hub_staging_resource_jobs')

    def preview_binding(self, actor, identity):
        """One SQLite read snapshot binds the resource and current grant revision.

        A previously returned binding is not a continuing access capability;
        the gateway must repeat this check on every ticket/asset request.
        """
        with self.repo._connection() as db:
            db.execute('BEGIN')
            item = self._resource(db, identity)
            self._authorize(db, actor, item['project'], 'read')
            self._authorize(db, actor, item['project'], 'preview')
            job = self._job(db, item['job_id'])
            if (item['kind'] != 'preview' or item['state'] != 'ready'
                    or item['artifact_digest'] is None or item['preview_path'] is None
                    or job.get('terminal') is not True
                    or any(p['reason'] == 'active_job' for p in item['protected'])
                    or any(job[k] != item[k] for k in ('project', 'created_by', 'source_commit'))):
                raise HubError('staging_preview_unavailable', 409)
            now = self._now()
            keep = item['keep']
            retained = keep and (keep['mode'] == 'pinned' or _seconds(keep['until']) > now)
            if not retained and (item['expires_at'] is None or _seconds(item['expires_at']) <= now):
                raise HubError('staging_preview_expired', 409)
            return item, self._policy(db, item['project'])['revision']

    def diagnostic_binding(self, actor, identity):
        """Current project read grant and retained terminal-failure artifact."""
        with self.repo._connection() as db:
            db.execute('BEGIN')
            item = self._resource(db, identity)
            self._authorize(db, actor, item['project'], 'read')
            job = self._job(db, item['job_id'])
            if (item['kind'] != 'test_run' or item['state'] != 'failed'
                    or item['artifact_digest'] is None or job.get('terminal') is not True
                    or any(p['reason'] == 'active_job' for p in item['protected'])
                    or any(job[k] != item[k] for k in ('project', 'created_by', 'source_commit'))):
                raise HubError('staging_diagnostic_unavailable', 409)
            now, keep = self._now(), item['keep']
            retained = keep and (keep['mode'] == 'pinned' or _seconds(keep['until']) > now)
            if not retained and (item['expires_at'] is None or _seconds(item['expires_at']) <= now):
                raise HubError('staging_diagnostic_unavailable', 409)
            return item, self._policy(db, item['project'])['revision']

    def list_resources(self, actor, project=None, *, kind=None, state=None, after=None, limit=100):
        _actor(actor)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HubError('invalid_staging_limit')
        if kind is not None and (type(kind) is not str or kind not in KINDS):
            raise HubError('invalid_staging_kind')
        if state is not None and (type(state) is not str or state not in STATES):
            raise HubError('invalid_staging_state')
        if after is not None:
            _uuid(after)
        with self.repo._connection() as db:
            if project is not None:
                self._authorize(db, actor, project, 'read')
            result = []
            for row in db.execute("SELECT e.data FROM hub_entities e JOIN hub_staging_resource_ownership o ON o.id=e.id WHERE e.kind='staging_resource' AND e.id>? ORDER BY e.id", (after or '',)):
                item = json.loads(row['data'])
                if project is not None and item['project'] != project or kind is not None and item['kind'] != kind or state is not None and item['state'] != state:
                    continue
                if state is None and item['state'] == 'deleted':
                    continue  # Compact history is available explicitly, not inventory clutter.
                try:
                    self._authorize(db, actor, item['project'], 'read')
                except HubError as exc:
                    if exc.code in ('staging_forbidden', 'project_archived'):
                        continue
                    raise
                result.append(item)
                if len(result) == limit + 1:
                    break
            return {'items': result[:limit],
                    'next_after': result[limit - 1]['id'] if len(result) > limit else None}

    def mark_resource_ready(self, principal, identity, expected_revision, *,
                            artifact_digest=None, size_bytes=None, preview_path=None, failed=False):
        self._trusted(principal)
        _digest(artifact_digest)
        if type(failed) is not bool or size_bytes is not None and (type(size_bytes) is not int or size_bytes < 0):
            raise HubError('invalid_staging_result')
        if preview_path is not None and (type(preview_path) is not str or
                re.fullmatch(r'/staging/previews/[a-zA-Z0-9_-]{1,128}/?', preview_path) is None):
            raise HubError('invalid_preview_path')
        with self.repo._transaction() as db:
            item = self._resource(db, identity)
            self.repo._project(item['project'], db)
            self.repo._cas(item, expected_revision)
            if item['state'] != 'provisioning':
                raise HubError('staging_resource_state_conflict', 409)
            if preview_path is not None and item['kind'] != 'preview':
                raise HubError('invalid_preview_path')
            if artifact_digest is not None and item['artifact_digest'] not in (None, artifact_digest):
                raise HubError('staging_artifact_binding_conflict', 409)
            item.update(state='failed' if failed else 'ready', size_bytes=size_bytes, preview_path=preview_path)
            if artifact_digest is not None:
                item['artifact_digest'] = artifact_digest
            if failed:
                item['protected'].append({'reason': 'latest_failure', 'ref': item['job_id']})
            return self._save(db, item['created_by'], 'staging.resource_result', item)

    @staticmethod
    def _protections(protected):
        if type(protected) is not list or len(protected) > 100:
            raise HubError('invalid_staging_protection')
        result = []
        for entry in protected:
            if type(entry) is not dict or set(entry) != {'reason', 'ref'} or type(entry['reason']) is not str or entry['reason'] not in PROTECTIONS - {'active_job'}:
                raise HubError('invalid_staging_protection')
            value = {'reason': entry['reason'], 'ref': _text(entry['ref'], 200)}
            if value not in result:
                result.append(value)
        return result

    def update_job(self, principal, job_id, expected_revision, *, terminal, protected=None):
        """Trusted terminal/protection reconciliation; never inferred from presence."""
        self._trusted(principal)
        if type(terminal) is not bool:
            raise HubError('invalid_staging_job_state')
        normalized = None if protected is None else self._protections(protected)
        with self.repo._transaction() as db:
            job = self._job(db, _uuid(job_id))
            self.repo._project(job['project'], db)
            self.repo._cas(job, expected_revision)
            if job['terminal'] and not terminal:
                raise HubError('staging_job_terminal', 409)
            protections = job['protected'] if normalized is None else normalized
            from . import reconciliation_context
            active_guard = reconciliation_context.current.get()
            if active_guard is not None:
                if active_guard.job_id != job_id:
                    raise reconciliation_context.Changed()
                # The same writer's guard has already checked malformed data,
                # ownership and the complete, bounded job-reference set.
                rows = db.execute("SELECT data FROM hub_entities WHERE kind='staging_resource' AND (CASE WHEN json_valid(data) THEN json_extract(data,'$.job_id') END)=? ORDER BY id LIMIT ?", (job_id, active_guard.max_resources + 1)).fetchall()
                if len(rows) > active_guard.max_resources:
                    raise reconciliation_context.Changed()
            else:
                rows = db.execute("SELECT data FROM hub_entities WHERE kind='staging_resource'")
            for row in rows:
                item = json.loads(row['data'])
                if item['job_id'] != job_id or item['state'] == 'deleted':
                    continue
                if item['state'] == 'deleting':
                    raise HubError('staging_cleanup_in_progress', 409)
                # Remove only the former job-wide protections. Resource-local
                # protection (including latest failure) requires an explicit
                # set_protection reconciliation and must not disappear here.
                own = [p for p in item['protected'] if p['reason'] != 'active_job'
                       and p not in job['protected']]
                item['protected'] = protections + [p for p in own if p not in protections]
                if not terminal:
                    item['protected'].append({'reason': 'active_job', 'ref': job_id})
                if terminal and not job['terminal'] and item['kind'] == 'workspace':
                    item['expires_at'] = _iso(self._now() + RETENTION['workspace'])
                self._save(db, job['created_by'], 'staging.resource_job_state', item)
            job.update(terminal=terminal, protected=protections)
            return self._save(db, job['created_by'], 'staging.job_state', job, table='hub_staging_resource_jobs')

    def set_protection(self, principal, identity, expected_revision, protected):
        self._trusted(principal)
        protected = self._protections(protected)
        with self.repo._transaction() as db:
            item = self._resource(db, identity)
            self.repo._project(item['project'], db)
            self.repo._cas(item, expected_revision)
            if item['state'] in ('deleting', 'deleted'):
                raise HubError('staging_cleanup_in_progress', 409)
            job = self._job(db, item['job_id'])
            # A per-resource update cannot remove an active/job-wide protection.
            item['protected'] = protected + [p for p in job['protected'] if p not in protected]
            if not job['terminal']:
                item['protected'].append({'reason': 'active_job', 'ref': job['id']})
            return self._save(db, item['created_by'], 'staging.resource_protection', item)

    def supersede_latest_failure(self, principal, job_id, *, superseded_by):
        """Remove an older terminal failure's protection only with ordered evidence.

        A delayed controller may resume after a newer failure has completed.
        Ordering, bindings and removal are checked in this same writer. Missing
        or ambiguous terminal evidence preserves all protections.
        """
        self._trusted(principal)
        _uuid(job_id); _uuid(superseded_by)
        if job_id == superseded_by:
            return
        with self.repo._transaction() as db:
            def terminal_failure(identity):
                row = db.execute('SELECT * FROM hub_work_jobs WHERE id=?', (identity,)).fetchone()
                if row is None or row['state'] != 'failed':
                    return None
                try:
                    target = json.loads(row['target'])
                    submission_id = target['submission_id']
                    entry = db.execute('SELECT id,data FROM hub_staging_submissions WHERE id=?', (submission_id,)).fetchone()
                    if entry is None:
                        return None
                    submission = json.loads(entry['data'])
                    job = self._job(db, identity)
                    if (submission.get('id') != entry['id'] or submission.get('job_id') != identity
                            or submission.get('phase') != 'verified' or submission.get('outcome') != 'failed'
                            or not valid_source_binding(submission) or job.get('terminal') is not True
                            or row['environment'] != 'staging' or row['requires_execution_proof'] != 1
                            or job.get('project') != row['project'] or submission.get('project') != row['project']
                            or job.get('created_by') != submission.get('actor') or row['actor'] != submission.get('actor')
                            or job.get('source_commit') != submission.get('source_commit')
                            or target.get('source_commit') != submission.get('source_commit')
                            or target.get('task_id') != submission.get('review_task_id')
                            or target.get('review_revision') != submission.get('review_task_revision')
                            or target.get('artifact_digest') != 'sha256:' + submission.get('source_digest', '')
                            or row['action_key'] != staging_action_key(submission['project'], submission['recipe'])):
                        return None
                    events = db.execute("SELECT sequence FROM hub_audit WHERE entity_id=? AND revision=? AND action IN ('work.failed','work.reconciled') ORDER BY sequence LIMIT 2",
                                        (identity, row['revision'])).fetchall()
                    if len(events) != 1:
                        return None
                    return submission, job, events[0]['sequence']
                except (KeyError, TypeError, ValueError, HubError):
                    return None

            older = terminal_failure(job_id)
            newer = terminal_failure(superseded_by)
            if older is None or newer is None:
                return
            prior_submission, job, prior_sequence = older
            next_submission, next_job, next_sequence = newer
            if (prior_submission['project'] != next_submission['project']
                    or prior_submission['recipe'] != next_submission['recipe']
                    or prior_sequence >= next_sequence
                    or type(next_job.get('protected')) is not list
                    or type(job.get('protected')) is not list
                    or {'reason': 'latest_failure', 'ref': superseded_by} not in next_job['protected']):
                return
            reference = {'reason': 'latest_failure', 'ref': job_id}
            rows = [json.loads(row['data']) for row in db.execute(
                "SELECT data FROM hub_entities WHERE kind='staging_resource'")]
            items = [item for item in rows if item['job_id'] == job_id]
            if reference not in job['protected'] and not any(reference in item['protected'] for item in items):
                return
            if reference in job['protected']:
                job['protected'] = [item for item in job['protected'] if item != reference]
                self._save(db, job['created_by'], 'staging.failure_superseded', job,
                           table='hub_staging_resource_jobs')
            for item in items:
                if item['state'] in ('deleting', 'deleted') or reference not in item['protected']:
                    continue
                item['protected'] = [protection for protection in item['protected'] if protection != reference]
                self._save(db, item['created_by'], 'staging.resource_protection', item)

    def set_keep(self, actor, identity, mode, *, until=None, reason='', expected_revision, admission_guard=None):
        _actor(actor)
        if mode not in ('until', 'pinned', 'none'):
            raise HubError('invalid_staging_keep')
        if mode != 'none':
            reason = _text(reason)
        if mode == 'until':
            until = _iso(_seconds(until))
        elif until is not None:
            raise HubError('invalid_staging_keep')
        with self.repo._transaction(before_commit=admission_guard) as db:
            item = self._resource(db, identity)
            self._authorize(db, actor, item['project'], 'retain')
            self._owned(actor, item)
            self.repo._cas(item, expected_revision)
            if actor in AGENTS and item['keep'] and item['keep']['by'] not in AGENTS:
                raise HubError('staging_human_keep_required', 403)
            if mode == 'pinned' and actor not in self.admin_actors:
                raise HubError('staging_admin_required', 403)
            if item['state'] in ('deleting', 'deleted'):
                raise HubError('staging_cleanup_in_progress', 409)
            if mode == 'none' and item['protected']:
                raise HubError('staging_resource_protected', 409)
            if item['keep'] and item['keep']['mode'] == 'pinned' and actor not in self.admin_actors:
                raise HubError('staging_admin_required', 403)
            now = self._now()
            if mode == 'until' and not now < _seconds(until) <= now + 90 * 86400:
                raise HubError('invalid_staging_keep_time')
            item['keep'] = None if mode == 'none' else {
                'mode': mode, 'until': until, 'by': actor, 'reason': reason, 'at': _iso(now)}
            return self._save(db, actor, 'staging.resource_keep', item)

    def _eligibility(self, db, actor, item, now):
        self._authorize(db, actor, item['project'], 'cleanup')
        ownership = db.execute('SELECT * FROM hub_staging_resource_ownership WHERE id=?', (item['id'],)).fetchone()
        if ownership is None or any(ownership[k] != item.get(k) for k in ('job_id', 'project', 'created_by', 'source_commit')):
            return 'unknown_ownership'
        if actor in AGENTS and actor != item['created_by']:
            return 'not_owned'
        if item['state'] not in ('ready', 'failed', 'expiring'):
            return item['state']
        try:
            job = self._job(db, item['job_id'])
        except HubError:
            return 'unknown_job'
        if any(job[k] != item[k] for k in ('project', 'created_by', 'source_commit')):
            return 'unknown_job_binding'
        if job.get('terminal') is not True:
            return 'active_job'
        if item['protected'] or job['protected']:
            return 'protected'
        keep = item['keep']
        if keep and (keep['mode'] == 'pinned' or _seconds(keep['until']) > now):
            return 'kept'
        if item['expires_at'] is None or _seconds(item['expires_at']) > now:
            return 'not_expired'
        return None

    def cleanup_plan(self, actor, project=None, *, kinds=None, admission_guard=None):
        _actor(actor)
        if kinds is not None and (type(kinds) not in (list, tuple, set, frozenset) or not kinds or any(type(k) is not str or k not in KINDS for k in kinds)):
            raise HubError('invalid_staging_kinds')
        with self.repo._transaction(before_commit=admission_guard) as db:
            if project is not None:
                self._authorize(db, actor, project, 'cleanup')
            now = self._now()
            remove, kept, revisions = [], [], {}
            for row in db.execute("SELECT data FROM hub_entities WHERE kind='staging_resource' ORDER BY id"):
                item = json.loads(row['data'])
                if item['state'] == 'deleted':
                    continue  # There are no retained bytes left to plan for.
                if project is not None and item['project'] != project or kinds is not None and item['kind'] not in kinds:
                    continue
                try:
                    why = self._eligibility(db, actor, item, now)
                except HubError as exc:
                    if exc.code in ('staging_forbidden', 'project_archived'):
                        continue
                    raise
                if why:
                    kept.append({'id': item['id'], 'why': why})
                else:
                    remove.append({'id': item['id'], 'kind': item['kind'],
                                   'size_bytes': item['size_bytes'], 'why': 'expired'})
                    revisions[item['id']] = item['revision']
            plan = {'plan_id': str(uuid.uuid4()), 'remove': remove, 'kept': kept,
                    'total_bytes': None if any(r['size_bytes'] is None for r in remove) else sum(r['size_bytes'] for r in remove),
                    'expires_at': _iso(now + 600)}
            digest = 'sha256:' + hashlib.sha256(_json([actor, plan, revisions]).encode()).hexdigest()
            plan['digest'] = digest
            stored = {'public': plan, 'revisions': revisions, 'begun': False, 'operations': [], 'skipped': []}
            db.execute('INSERT INTO hub_staging_cleanup_plans(id,actor,data) VALUES(?,?,?)',
                       (plan['plan_id'], actor, _json(stored)))
            return plan

    def cleanup_begin(self, actor, plan_id, digest, *, admission_guard=None):
        _actor(actor)
        with self.repo._transaction(before_commit=admission_guard) as db:
            row = db.execute('SELECT * FROM hub_staging_cleanup_plans WHERE id=?', (_uuid(plan_id),)).fetchone()
            if row is None:
                raise HubError('not_found', 404)
            stored = json.loads(row['data'])
            if row['actor'] != actor:
                raise HubError('staging_forbidden', 403)
            if type(digest) is not str or stored['public']['digest'] != digest:
                raise HubError('staging_plan_digest_conflict', 409)
            # Recheck scope even on replay; revoked actors cannot retrieve dispatch.
            for entry in stored['public']['remove']:
                item = self._resource(db, entry['id'])
                self._authorize(db, actor, item['project'], 'cleanup')
            if stored['begun']:
                return {'plan_id': plan_id, 'operations': stored['operations'],
                        'skipped': stored['skipped'], 'dispatch': False, 'reconcile_required': True}
            if self._now() >= _seconds(stored['public']['expires_at']):
                raise HubError('staging_plan_expired', 409)
            for entry in stored['public']['remove']:
                item = self._resource(db, entry['id'])
                why = self._eligibility(db, actor, item, self._now())
                if item['revision'] != stored['revisions'][item['id']] or why:
                    stored['skipped'].append({'id': item['id'], 'why': why or 'changed'})
                    continue
                previous_state = item['state']
                item['state'] = 'deleting'
                self._save(db, actor, 'staging.cleanup_begin', item)
                stored['operations'].append({'id': item['id'], 'kind': item['kind'],
                    'project': item['project'], 'job_id': item['job_id'],
                    'expected_revision': item['revision'], 'operation_id': str(uuid.uuid4()),
                    'previous_state': previous_state})
            stored['begun'] = True
            db.execute('UPDATE hub_staging_cleanup_plans SET data=? WHERE id=?', (_json(stored), plan_id))
            return {'plan_id': plan_id, 'operations': stored['operations'],
                    'skipped': stored['skipped'], 'dispatch': True, 'reconcile_required': False}

    def cleanup_not_dispatched(self, principal, plan_id, identity, expected_revision, *, receipt_id, reason):
        """Trusted local proof of a durable preflight that never crossed POST.

        An absent/legacy prior-state record or any dispatching/unknown journal
        cannot authorize restoration. The receipt, restoration and terminal
        not-dispatched journal state commit together under the live Hub owner.
        """
        if reason not in ('preflight_failed', 'preflight_cancelled', 'preflight_interrupted'):
            raise HubError('invalid_cleanup_receipt')
        return self._cleanup_restore(principal, plan_id, identity, expected_revision,
                                     receipt_id, reason, 'not_dispatched')

    def cleanup_not_accepted(self, principal, plan_id, identity, expected_revision, *, receipt_id, reason):
        """A fixed worker rejection of the first POST is not a no-POST claim."""
        if reason not in ('worker_busy', 'cleanup_revision_or_reference', 'resource_job_not_terminal',
                          'peer_not_allowed', 'unauthorized'):
            raise HubError('invalid_cleanup_receipt')
        return self._cleanup_restore(principal, plan_id, identity, expected_revision,
                                     receipt_id, reason, 'not_accepted')

    def _cleanup_restore(self, principal, plan_id, identity, expected_revision, receipt_id, reason, proof):
        self._trusted(principal)
        _uuid(receipt_id)
        fingerprint = _json([proof, plan_id, identity, expected_revision, reason])
        with self.repo._transaction() as db:
            prior = db.execute('SELECT * FROM hub_staging_cleanup_receipts WHERE id=?', (receipt_id,)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise HubError('staging_receipt_conflict', 409)
                return json.loads(prior['data'])
            row = db.execute('SELECT * FROM hub_staging_cleanup_plans WHERE id=?', (_uuid(plan_id),)).fetchone()
            if row is None:
                raise HubError('not_found', 404)
            plan = json.loads(row['data'])
            operation = next((item for item in plan['operations'] if item['id'] == identity
                and item['expected_revision'] == expected_revision and item['operation_id'] == receipt_id), None)
            if (not plan['begun'] or operation is None
                    or operation.get('previous_state') not in ('ready', 'failed', 'expiring')):
                raise HubError('staging_cleanup_outcome_uncertain', 409)
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_staging_dispatch_cleanup'").fetchone():
                raise HubError('staging_cleanup_outcome_uncertain', 409)
            row_dispatch = db.execute('SELECT data FROM hub_staging_dispatch_cleanup WHERE id=?', (receipt_id,)).fetchone()
            dispatch = json.loads(row_dispatch['data']) if row_dispatch else None
            if (not dispatch or dispatch.get('state') != ('preflight' if proof == 'not_dispatched' else 'rejected')
                    or dispatch.get('plan_id') != plan_id or dispatch.get('resource_id') != identity
                    or dispatch.get('resource_revision') != expected_revision):
                raise HubError('staging_cleanup_outcome_uncertain', 409)
            if proof == 'not_dispatched':
                if 'payload' in dispatch:
                    raise HubError('staging_cleanup_outcome_uncertain', 409)
            else:
                payload = dispatch.get('payload')
                if (dispatch.get('reason') != reason or type(payload) is not dict
                        or set(payload) != {'command_id', 'idempotency_key', 'resources'}
                        or payload['command_id'] != receipt_id or payload['idempotency_key'] != receipt_id
                        or type(payload['resources']) is not list or len(payload['resources']) != 1
                        or type(payload['resources'][0]) is not dict
                        or set(payload['resources'][0]) != {'resource_id', 'expected_revision'}
                        or payload['resources'][0]['resource_id'] != identity
                        or type(payload['resources'][0]['expected_revision']) is not int
                        or payload['resources'][0]['expected_revision'] < 1):
                    raise HubError('staging_cleanup_outcome_uncertain', 409)
            item = self._resource(db, identity)
            self.repo._cas(item, expected_revision)
            if item['state'] != 'deleting':
                raise HubError('staging_resource_state_conflict', 409)
            item['state'] = operation['previous_state']
            result = self._save(db, row['actor'], 'staging.cleanup_' + proof, item)
            db.execute('INSERT INTO hub_staging_cleanup_receipts VALUES(?,?,?)',
                       (receipt_id, fingerprint, _json(result)))
            dispatch.update(state=proof, reason=reason)
            db.execute('UPDATE hub_staging_dispatch_cleanup SET data=? WHERE id=?', (_json(dispatch), receipt_id))
            return result

    def cleanup_finish(self, principal, plan_id, identity, expected_revision, *,
                       absent, receipt_id, error=None):
        """Controller verifies worker provenance/absence before calling this.

        A non-absence receipt never authorizes retry. Metadata and the durable
        operation/receipt are retained for manual reconciliation.
        """
        self._trusted(principal)
        _uuid(receipt_id)
        if type(absent) is not bool:
            raise HubError('invalid_cleanup_receipt')
        if error is not None:
            error = _text(error, 200)
        fingerprint = _json([plan_id, identity, expected_revision, absent, error])
        with self.repo._transaction() as db:
            prior = db.execute('SELECT * FROM hub_staging_cleanup_receipts WHERE id=?', (receipt_id,)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise HubError('staging_receipt_conflict', 409)
                return json.loads(prior['data'])
            row = db.execute('SELECT * FROM hub_staging_cleanup_plans WHERE id=?', (_uuid(plan_id),)).fetchone()
            if row is None:
                raise HubError('not_found', 404)
            stored = json.loads(row['data'])
            operation = next((r for r in stored['operations'] if r['id'] == identity and r['expected_revision'] == expected_revision), None)
            if not stored['begun'] or operation is None:
                raise HubError('staging_cleanup_not_begun', 409)
            item = self._resource(db, identity)
            # Revocation stops new effects, not recording a trusted already-issued
            # operation's outcome; recording absence does not touch the host.
            self.repo._cas(item, expected_revision)
            if item['state'] != 'deleting':
                raise HubError('staging_resource_state_conflict', 409)
            item.update(state='deleted' if absent else 'delete_failed')
            result = self._save(db, row['actor'], 'staging.cleanup_finish', item)
            db.execute('INSERT INTO hub_staging_cleanup_receipts(id,fingerprint,data) VALUES(?,?,?)',
                       (receipt_id, fingerprint, _json(result)))
            return result
