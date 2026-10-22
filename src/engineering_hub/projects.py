"""Human-owned project metadata; not an executor registry or permission grant.

No project operation changes the independent work-queue execution allowlists.
Archiving retains all metadata, tasks, decisions, notes and audit history.
"""
from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import re
from .entity_context import _url

from .repository import AGENTS, HubError, HubRepository, _json

_UNSET = object()
_NAMES = {'engineering-hub': 'Engineering Hub', '3d-viewer': '3D Viewer',
          'project-alpha': 'Project Alpha', 'ledge-top-ops': 'LedgeTop Ops',
          'treedar': 'TreeDar'}


class ProjectRepository:
    def __init__(self, repo):
        if type(repo) is not HubRepository:
            raise TypeError('An existing HubRepository is required')
        self.repo = repo
        with self._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS hub_projects (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS hub_project_registry_meta (key TEXT PRIMARY KEY)')
            db.execute('CREATE TABLE IF NOT EXISTS hub_project_requests (actor TEXT NOT NULL, key TEXT NOT NULL, '
                       'fingerprint TEXT NOT NULL, project_id TEXT NOT NULL, PRIMARY KEY(actor,key))')
            if not db.execute("SELECT 1 FROM hub_project_registry_meta WHERE key='seeded-v1'").fetchone():
                now = datetime.now(timezone.utc).isoformat()
                for identity, name in _NAMES.items():
                    item = dict(id=identity, name=name, description='', repository_url=None,
                                lead_agent=None, status='on_track', parent_project=None,
                                archived=False, revision=1, created_at=now, updated_at=now)
                    db.execute('INSERT OR IGNORE INTO hub_projects VALUES(?,?)', (identity, _json(item)))
                db.execute("INSERT INTO hub_project_registry_meta VALUES('seeded-v1')")

    @staticmethod
    def _identity(value):
        if type(value) is not str or re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', value) is None:
            raise HubError('invalid_project')
        return value

    def _transaction(self):
        return self.repo._transaction()

    def _connection(self):
        return self.repo._connection()

    @contextmanager
    def knowledge_write_admission(self, project):
        """Serialize registered-project note admission with archive updates.

        Hold the Hub writer before the separate knowledge writer, never the
        reverse. This orders admission, not a joint two-database transaction:
        a committed note survives a later Hub guard-exit or ownership failure.
        Existing knowledge namespaces are not implicitly registered projects.
        """
        from agent_message_bus.knowledge import KnowledgeProjectArchived
        with self._transaction() as db:
            row = db.execute('SELECT data FROM hub_projects WHERE id=?', (project,)).fetchone()
            if row is not None:
                item = json.loads(row['data'])
                if type(item.get('archived')) is not bool:
                    raise RuntimeError('Invalid project admission record')
                if item['archived']:
                    raise KnowledgeProjectArchived()
            yield

    _text = staticmethod(HubRepository._text)
    _actor = staticmethod(HubRepository._actor)
    _cas = staticmethod(HubRepository._cas)

    def _fields(self, name, description, repository_url, lead_agent, status, parent_project):
        name = self._text(name, 100)
        if (type(description) is not str or len(description) > 4096
                or any((ord(c) < 32 and c not in '\n\r\t') or ord(c) == 127 for c in name + description)):
            raise HubError('invalid_project_metadata')
        try:
            (name + description).encode('utf-8')
        except UnicodeError:
            raise HubError('invalid_project_metadata') from None
        if repository_url is not None:
            try:
                repository_url = _url(repository_url)
            except (ValueError, TypeError, AttributeError):
                raise HubError('invalid_repository_url') from None
        if lead_agent is not None and (type(lead_agent) is not str or lead_agent not in AGENTS):
            raise HubError('invalid_agent')
        if type(status) is not str or status not in {'on_track', 'at_risk', 'off_track', 'paused'}:
            raise HubError('invalid_project_status')
        if parent_project is not None:
            self._identity(parent_project)
        return dict(name=name, description=description, repository_url=repository_url,
                    lead_agent=lead_agent, status=status, parent_project=parent_project)

    @staticmethod
    def _fetch(db, identity):
        row = db.execute('SELECT data FROM hub_projects WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise HubError('not_found', 404)
        return json.loads(row['data'])

    def _parent(self, db, identity, parent):
        seen = {identity}
        while parent is not None:
            if parent in seen:
                raise HubError('project_parent_cycle', 409)
            seen.add(parent)
            item = self._fetch(db, parent)
            if item['archived']:
                raise HubError('project_archived', 409)
            parent = item['parent_project']

    def _save_project(self, db, actor, action, item):
        item['updated_at'] = datetime.now(timezone.utc).isoformat()
        db.execute('INSERT INTO hub_projects VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                   (item['id'], _json(item)))
        db.execute('INSERT INTO hub_audit(actor,action,entity_id,revision,created_at) VALUES(?,?,?,?,?)',
                   (actor, action, item['id'], item['revision'], item['updated_at']))
        self.repo._emit(db, action, item)
        return item

    def list_projects(self, include_archived=False):
        if type(include_archived) is not bool:
            raise HubError('invalid_input')
        with self._connection() as db:
            items = [json.loads(row['data']) for row in db.execute('SELECT data FROM hub_projects ORDER BY id')]
        return [item for item in items if include_archived or not item['archived']]

    def get_project(self, project_id):
        self._identity(project_id)
        with self._connection() as db:
            return self._fetch(db, project_id)

    def create_project(self, actor, project_id, name, client_request_id, *, description='',
                       repository_url=None, lead_agent=None, status='on_track', parent_project=None):
        self._actor(actor, human=True)
        self._identity(project_id)
        key = self._text(client_request_id)
        fields = self._fields(name, description, repository_url, lead_agent, status, parent_project)
        fingerprint = hashlib.sha256(_json([project_id, fields]).encode()).hexdigest()
        with self._transaction() as db:
            prior = db.execute('SELECT * FROM hub_project_requests WHERE actor=? AND key=?', (actor, key)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise HubError('idempotency_conflict', 409)
                return self._fetch(db, prior['project_id'])
            if db.execute('SELECT 1 FROM hub_projects WHERE id=?', (project_id,)).fetchone():
                raise HubError('project_exists', 409)
            if db.execute('SELECT COUNT(*) FROM hub_projects').fetchone()[0] >= 1000:
                raise HubError('project_limit', 409)
            self._parent(db, project_id, parent_project)
            item = dict(id=project_id, **fields, archived=False, revision=1,
                        created_at=datetime.now(timezone.utc).isoformat())
            self._save_project(db, actor, 'project.created', item)
            db.execute('INSERT INTO hub_project_requests VALUES(?,?,?,?)', (actor, key, fingerprint, project_id))
            return item

    def update_project(self, actor, project_id, expected_revision, *, name=_UNSET,
                       description=_UNSET, repository_url=_UNSET, lead_agent=_UNSET,
                       status=_UNSET, parent_project=_UNSET):
        self._actor(actor, human=True)
        self._identity(project_id)
        changes = dict(name=name, description=description, repository_url=repository_url,
                       lead_agent=lead_agent, status=status, parent_project=parent_project)
        with self._transaction() as db:
            item = self._fetch(db, project_id)
            self._cas(item, expected_revision)
            if item['archived']:
                raise HubError('project_archived', 409)
            fields = self._fields(**{key: item[key] if value is _UNSET else value for key, value in changes.items()})
            self._parent(db, project_id, fields['parent_project'])
            item.update(fields, revision=item['revision'] + 1)
            return self._save_project(db, actor, 'project.updated', item)

    def archive_project(self, actor, project_id, expected_revision):
        self._actor(actor, human=True)
        self._identity(project_id)
        if project_id == 'engineering-hub':
            raise HubError('system_project_required', 409)
        with self._transaction() as db:
            item = self._fetch(db, project_id)
            self._cas(item, expected_revision)
            if item['archived']:
                return item
            tasks = {json.loads(row['data'])['id'] for row in db.execute(
                "SELECT data FROM hub_entities WHERE kind='task'")
                if json.loads(row['data']).get('project') == project_id}
            for row in db.execute("SELECT data FROM hub_entities WHERE kind='run'"):
                run = json.loads(row['data'])
                if (run.get('task_id') in tasks or run.get('target') == project_id + ':staging') and run.get('status') in {'pending', 'running', 'held', 'failed'}:
                    raise HubError('active_run', 409)
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_work_jobs'").fetchone():
                if db.execute("SELECT 1 FROM hub_work_jobs WHERE project=? AND state NOT IN ('succeeded','failed','cancelled') LIMIT 1",
                              (project_id,)).fetchone():
                    raise HubError('active_job', 409)
            item.update(archived=True, revision=item['revision'] + 1)
            return self._save_project(db, actor, 'project.archived', item)
