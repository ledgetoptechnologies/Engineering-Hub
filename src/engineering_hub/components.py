"""Dynamic human-owned project component catalog; inert metadata, not authority.

Components describe products/packages inside a project. They are not tasks,
subprojects, execution targets, access grants or deployment configuration.
Retirement preserves IDs, slugs, legacy links and audit history.
"""
from datetime import datetime, timezone
import hashlib
import json
import re
import uuid

from .entity_context import _url
from .repository import HubError, HubRepository, _json


KINDS = frozenset({'web_app', 'desktop_app', 'mobile_app', 'service',
                   'package', 'library', 'tool', 'other'})
STATUSES = frozenset({'active', 'planned', 'paused', 'retired'})
_UNSET = object()


class ProjectComponentRepository:
    def __init__(self, repo):
        if type(repo) is not HubRepository:
            raise TypeError('An existing HubRepository is required')
        self.repo = repo
        with repo._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS hub_project_components '
                       '(id TEXT PRIMARY KEY, project TEXT NOT NULL, slug TEXT NOT NULL, '
                       'data TEXT NOT NULL, UNIQUE(project,slug))')
            db.execute('CREATE TABLE IF NOT EXISTS hub_component_requests '
                       '(actor TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL, '
                       'component_id TEXT NOT NULL, PRIMARY KEY(actor,key))')

    def _project(self, db, project, *, active):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_projects'").fetchone():
            raise HubError('project_registry_required', 409)
        self.repo._project(project, db, require_active=active)

    @staticmethod
    def _identity(value):
        try:
            if type(value) is str and str(uuid.UUID(value)) == value:
                return value
        except ValueError:
            pass
        raise HubError('invalid_component_id')

    @staticmethod
    def _slug(value):
        if type(value) is not str or re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', value) is None:
            raise HubError('invalid_component_slug')
        return value

    def _fields(self, name, description, kind, status, repository_url, package_url, legacy_project_id):
        name = self.repo._text(name, 100)
        if (type(description) is not str or len(description) > 4096
                or any((ord(char) < 32 and char not in '\n\r\t') or ord(char) == 127
                       for char in name + description)):
            raise HubError('invalid_component_metadata')
        try:
            (name + description).encode('utf-8')
        except UnicodeError:
            raise HubError('invalid_component_metadata') from None
        if type(kind) is not str or kind not in KINDS:
            raise HubError('invalid_component_kind')
        if type(status) is not str or status not in STATUSES:
            raise HubError('invalid_component_status')
        urls = {}
        for field, value in (('repository_url', repository_url), ('package_url', package_url)):
            try:
                urls[field] = None if value is None else _url(value)
            except (ValueError, TypeError, AttributeError):
                raise HubError('invalid_component_url') from None
        if legacy_project_id is not None:
            self._slug(legacy_project_id)
        return dict(name=name, description=description, kind=kind, status=status,
                    legacy_project_id=legacy_project_id, **urls)

    @staticmethod
    def _fetch(db, identity):
        row = db.execute('SELECT data FROM hub_project_components WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise HubError('not_found', 404)
        return json.loads(row['data'])

    def _save(self, db, actor, action, item):
        item['updated_at'] = datetime.now(timezone.utc).isoformat()
        db.execute('INSERT INTO hub_project_components(id,project,slug,data) VALUES(?,?,?,?) '
                   'ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                   (item['id'], item['project'], item['slug'], _json(item)))
        db.execute('INSERT INTO hub_audit(actor,action,entity_id,revision,created_at) VALUES(?,?,?,?,?)',
                   (actor, action, item['id'], item['revision'], item['updated_at']))
        self.repo._emit(db, action, item)
        return item

    def create_component(self, actor, project, slug, name, client_request_id, *,
                         description='', kind='other', status='planned',
                         repository_url=None, package_url=None, legacy_project_id=None):
        self.repo._actor(actor, human=True)
        self._slug(slug)
        key = self.repo._text(client_request_id, 128)
        fields = self._fields(name, description, kind, status, repository_url, package_url, legacy_project_id)
        fingerprint = hashlib.sha256(_json([project, slug, fields]).encode()).hexdigest()
        with self.repo._transaction() as db:
            self._project(db, project, active=True)
            if legacy_project_id is not None:
                self._project(db, legacy_project_id, active=False)
            prior = db.execute('SELECT * FROM hub_component_requests WHERE actor=? AND key=?', (actor, key)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise HubError('idempotency_conflict', 409)
                return self._fetch(db, prior['component_id'])
            if db.execute('SELECT 1 FROM hub_project_components WHERE project=? AND slug=?', (project, slug)).fetchone():
                raise HubError('component_exists', 409)
            if (db.execute('SELECT count(*) FROM hub_project_components WHERE project=?', (project,)).fetchone()[0] >= 1000
                    or db.execute('SELECT count(*) FROM hub_project_components').fetchone()[0] >= 10000):
                raise HubError('component_limit', 409)
            item = dict(id=str(uuid.uuid4()), project=project, slug=slug, **fields,
                        created_by=actor, created_at=datetime.now(timezone.utc).isoformat(), revision=1)
            self._save(db, actor, 'component.created', item)
            db.execute('INSERT INTO hub_component_requests VALUES(?,?,?,?)', (actor, key, fingerprint, item['id']))
            return item

    def get_component(self, identity):
        self._identity(identity)
        with self.repo._connection() as db:
            item = self._fetch(db, identity)
            self._project(db, item['project'], active=False)
            return item

    def list_components(self, project, *, include_retired=False, kind=None, status=None, after=None, limit=100):
        if type(include_retired) is not bool or type(limit) is not int or not 1 <= limit <= 100:
            raise HubError('invalid_component_list')
        if kind is not None and (type(kind) is not str or kind not in KINDS):
            raise HubError('invalid_component_kind')
        if status is not None and (type(status) is not str or status not in STATUSES):
            raise HubError('invalid_component_status')
        if after is not None:
            self._identity(after)
        with self.repo._connection() as db:
            self._project(db, project, active=False)
            result = []
            for row in db.execute('SELECT data FROM hub_project_components WHERE project=? AND id>? ORDER BY id',
                                  (project, after or '')):
                item = json.loads(row['data'])
                if (not include_retired and item['status'] == 'retired'
                        or kind is not None and item['kind'] != kind
                        or status is not None and item['status'] != status):
                    continue
                result.append(item)
                if len(result) == limit + 1:
                    break
            return {'items': result[:limit],
                    'next_after': result[limit - 1]['id'] if len(result) > limit else None}

    def update_component(self, actor, identity, expected_revision, *, name=_UNSET,
                         description=_UNSET, kind=_UNSET, status=_UNSET,
                         repository_url=_UNSET, package_url=_UNSET, legacy_project_id=_UNSET):
        self.repo._actor(actor, human=True)
        self._identity(identity)
        changes = dict(name=name, description=description, kind=kind, status=status,
                       repository_url=repository_url, package_url=package_url, legacy_project_id=legacy_project_id)
        if all(value is _UNSET for value in changes.values()):
            raise HubError('component_changes_required')
        with self.repo._transaction() as db:
            item = self._fetch(db, identity)
            self._project(db, item['project'], active=True)
            self.repo._cas(item, expected_revision)
            fields = self._fields(**{key: item[key] if value is _UNSET else value for key, value in changes.items()})
            if fields['legacy_project_id'] is not None:
                self._project(db, fields['legacy_project_id'], active=False)
            if all(item[key] == value for key, value in fields.items()):
                return item
            action = 'component.retired' if fields['status'] == 'retired' and item['status'] != 'retired' else 'component.updated'
            item.update(fields, revision=item['revision'] + 1)
            return self._save(db, actor, action, item)

    def retire_component(self, actor, identity, expected_revision):
        return self.update_component(actor, identity, expected_revision, status='retired')
