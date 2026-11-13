"""Bounded, declarative human reviews. No uploaded code, file proxy or runtime.

An explicit current project grant is required for every operation. Catalog
metadata is never a permission grant. Dataset/schema/component bindings are
immutable; component changes require a new review rather than silently applying
old answers to a new version. Old rows and answer events remain in the ledger.
"""
from datetime import datetime, timezone
import hashlib
import json
import re
import uuid

from .components import ProjectComponentRepository
from .repository import HubError, HubRepository, _json


SCHEMA = 'choice-text.v1'
MAX_MANIFEST = 1024 * 1024
MAX_REVIEW_BYTES = 4 * 1024 * 1024
_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z')


def _id(value):
    try:
        if type(value) is str and str(uuid.UUID(value)) == value:
            return value
    except ValueError:
        pass
    raise HubError('invalid_review_id')


def _text(value, maximum, *, empty=False):
    if (type(value) is not str or len(value) > maximum or not empty and not value.strip()
            or any(ord(c) < 32 and c not in '\n\r\t' or ord(c) == 127 for c in value)):
        raise HubError('invalid_review_text')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise HubError('invalid_review_text') from None
    return value.strip()


def _key(value):
    if type(value) is not str or _KEY.fullmatch(value) is None:
        raise HubError('invalid_review_item_id')
    return value


class InteractiveReviewRepository:
    def __init__(self, repo, components, *, authorize=None, asset_authorize=None):
        if (type(repo) is not HubRepository or type(components) is not ProjectComponentRepository
                or components.repo is not repo or authorize is not None and not callable(authorize)
                or asset_authorize is not None and not callable(asset_authorize)):
            raise TypeError('Explicit same-ledger component repository and authorization callbacks required')
        self.repo, self.components = repo, components
        self.authorize, self.asset_authorize = authorize, asset_authorize
        with repo._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS hub_interactive_reviews '
                       '(id TEXT PRIMARY KEY, project TEXT NOT NULL, component_id TEXT NOT NULL, data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS hub_interactive_review_requests '
                       '(actor TEXT NOT NULL,key TEXT NOT NULL,fingerprint TEXT NOT NULL,review_id TEXT NOT NULL,PRIMARY KEY(actor,key))')
            db.execute('CREATE TABLE IF NOT EXISTS hub_interactive_review_answer_events '
                       '(review_id TEXT NOT NULL,revision INTEGER NOT NULL,item_id TEXT NOT NULL,actor TEXT NOT NULL,'
                       'value TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(review_id,revision,item_id))')

    def _allow(self, actor, project, action, db):
        if type(actor) is not str:
            raise HubError('forbidden', 403)
        self.repo._actor(actor)
        if self.authorize is None or self.authorize(actor, project, action) is not True:
            raise HubError('interactive_review_forbidden', 403)
        self.components._project(db, project, active=True)

    def _component(self, db, project, component_id, revision=None):
        component = self.components._fetch(db, _id(component_id))
        if component['project'] != project:
            raise HubError('review_component_scope', 403)
        if component['status'] != 'active':
            raise HubError('review_component_disabled', 409)
        if revision is not None and (type(revision) is not int or component['revision'] != revision):
            raise HubError('review_component_changed', 409)
        return component

    def _assets(self, actor, project, component_id, items):
        for item in items:
            asset = item.get('asset_id')
            if asset is not None and (self.asset_authorize is None
                    or self.asset_authorize(actor, project, component_id, asset) is not True):
                raise HubError('review_asset_not_registered', 403)

    def _items(self, items):
        if type(items) is not list or not 1 <= len(items) <= 1000:
            raise HubError('invalid_review_items')
        result, identities = [], set()
        for item in items:
            if type(item) is not dict or not {'id', 'title', 'prompt'} <= set(item) or set(item) - {'id', 'title', 'prompt', 'choices', 'asset_id'}:
                raise HubError('invalid_review_item')
            identity = _key(item['id'])
            if identity in identities:
                raise HubError('duplicate_review_item')
            identities.add(identity)
            normalized = dict(id=identity, title=_text(item['title'], 100), prompt=_text(item['prompt'], 4096))
            if item.get('choices') is not None:
                choices = item['choices']
                if type(choices) is not list or not 2 <= len(choices) <= 20:
                    raise HubError('invalid_review_choices')
                normalized['choices'], seen = [], set()
                for choice in choices:
                    if type(choice) is not dict or set(choice) != {'id', 'label'}:
                        raise HubError('invalid_review_choices')
                    key = _key(choice['id'])
                    if key in seen:
                        raise HubError('duplicate_review_choice')
                    seen.add(key)
                    normalized['choices'].append({'id': key, 'label': _text(choice['label'], 100)})
            if item.get('asset_id') is not None:
                normalized['asset_id'] = _id(item['asset_id'])
            result.append(normalized)
        if len(_json(result).encode()) > MAX_MANIFEST:
            raise HubError('review_manifest_too_large', 413)
        return result

    @staticmethod
    def _fetch(db, identity):
        row = db.execute('SELECT data FROM hub_interactive_reviews WHERE id=?', (_id(identity),)).fetchone()
        if row is None:
            raise HubError('not_found', 404)
        return json.loads(row['data'])

    def _checked(self, db, actor, identity, action):
        item = self._fetch(db, identity)
        self._allow(actor, item['project'], action, db)
        self._component(db, item['project'], item['component_id'], item['component_revision'])
        self._assets(actor, item['project'], item['component_id'], item['items'])
        return item

    def _save(self, db, actor, action, item):
        item['updated_at'] = datetime.now(timezone.utc).isoformat()
        encoded = _json(item)
        if len(encoded.encode('utf-8')) > MAX_REVIEW_BYTES:
            raise HubError('review_state_too_large', 413)
        db.execute('INSERT INTO hub_interactive_reviews VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                   (item['id'], item['project'], item['component_id'], encoded))
        db.execute('INSERT INTO hub_audit(actor,action,entity_id,revision,created_at) VALUES(?,?,?,?,?)',
                   (actor, action, item['id'], item['revision'], item['updated_at']))
        # Global event/activity consumers may not possess the review grant.
        # Emit an inert pointer only; never manifest, title, dataset or answers.
        self.repo._emit(db, action, {key: item[key] for key in
            ('id', 'project', 'component_id', 'revision', 'state', 'updated_at')})
        return item

    def _request(self, db, actor, key, fingerprint):
        row = db.execute('SELECT * FROM hub_interactive_review_requests WHERE actor=? AND key=?', (actor, key)).fetchone()
        if row and row['fingerprint'] != fingerprint:
            raise HubError('idempotency_conflict', 409)
        return row

    def create_review(self, actor, project, component_id, component_revision, topic,
                      dataset_digest, title, items, client_request_id, *, schema=SCHEMA):
        self.repo._actor(actor, agent=True)
        key = _text(client_request_id, 128)
        title, topic = _text(title, 100), _text(topic, 200)
        if schema != SCHEMA or type(schema) is not str:
            raise HubError('invalid_review_schema')
        if type(dataset_digest) is not str or re.fullmatch(r'sha256:[a-f0-9]{64}', dataset_digest) is None:
            raise HubError('invalid_review_dataset_digest')
        items = self._items(items)
        fingerprint = hashlib.sha256(_json(['create', project, component_id, component_revision,
            topic, dataset_digest, title, schema, items]).encode()).hexdigest()
        with self.repo._transaction() as db:
            self._allow(actor, project, 'review_create', db)
            self._allow(actor, project, 'review_read', db)
            self._component(db, project, component_id, component_revision)
            self._assets(actor, project, component_id, items)
            prior = self._request(db, actor, key, fingerprint)
            if prior:
                return self._checked(db, actor, prior['review_id'], 'review_read')
            if (db.execute('SELECT count(*) FROM hub_interactive_reviews WHERE project=?', (project,)).fetchone()[0] >= 1000
                    or db.execute('SELECT count(*) FROM hub_interactive_reviews').fetchone()[0] >= 10000):
                raise HubError('interactive_review_limit', 409)
            item = dict(id=str(uuid.uuid4()), project=project, component_id=component_id,
                component_revision=component_revision, topic=topic, dataset_digest=dataset_digest,
                schema=schema, title=title, items=items, state='open', answers={}, created_by=actor,
                created_at=datetime.now(timezone.utc).isoformat(), revision=1)
            self._save(db, actor, 'interactive_review.created', item)
            db.execute('INSERT INTO hub_interactive_review_requests VALUES(?,?,?,?)', (actor, key, fingerprint, item['id']))
            return item

    def get_review(self, actor, identity):
        with self.repo._connection() as db:
            return self._checked(db, actor, identity, 'review_read')

    @staticmethod
    def _summary(item):
        return {**{key: value for key, value in item.items() if key not in ('items', 'answers')},
                'item_count': len(item['items']), 'answered_count': len(item['answers'])}

    def get_state(self, actor, identity):
        item = self.get_review(actor, identity)
        return dict(self._summary(item), answers=item['answers'])

    def list_reviews(self, actor, project, *, component_id=None, state=None, after=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100 or state not in (None, 'open', 'completed'):
            raise HubError('invalid_review_list')
        if after is not None:
            _id(after)
        with self.repo._connection() as db:
            self._allow(actor, project, 'review_read', db)
            if component_id is not None:
                self._component(db, project, component_id)
            result = []
            for row in db.execute('SELECT data FROM hub_interactive_reviews WHERE project=? AND id>? ORDER BY id', (project, after or '')):
                item = json.loads(row['data'])
                if component_id is not None and item['component_id'] != component_id or state is not None and item['state'] != state:
                    continue
                try:
                    self._component(db, project, item['component_id'], item['component_revision'])
                    self._assets(actor, project, item['component_id'], item['items'])
                except HubError as error:
                    if error.code in ('review_component_changed', 'review_component_disabled', 'review_asset_not_registered'):
                        continue
                    raise
                result.append(self._summary(item))
                if len(result) == limit + 1:
                    break
            return {'items': result[:limit], 'next_after': result[limit - 1]['id'] if len(result) > limit else None}

    def answer_review(self, actor, identity, expected_revision, client_request_id, answers, *, complete=False, admission_guard=None):
        self.repo._actor(actor, human=True)
        key = _text(client_request_id, 128)
        if type(complete) is not bool or type(answers) is not list or len(answers) > 100 or not answers and not complete:
            raise HubError('invalid_review_answers')
        normalized, seen = [], set()
        for answer in answers:
            if type(answer) is not dict or set(answer) != {'item_id', 'value'}:
                raise HubError('invalid_review_answer')
            item_id = _key(answer['item_id'])
            if item_id in seen:
                raise HubError('duplicate_review_answer')
            seen.add(item_id)
            normalized.append({'item_id': item_id, 'value': _text(answer['value'], 2000)})
        fingerprint = hashlib.sha256(_json(['answer', identity, expected_revision, normalized, complete]).encode()).hexdigest()
        with self.repo._transaction(before_commit=admission_guard) as db:
            item = self._checked(db, actor, identity, 'review_answer')
            self._allow(actor, item['project'], 'review_read', db)
            prior = self._request(db, actor, key, fingerprint)
            if prior:
                return item
            self.repo._cas(item, expected_revision)
            if item['state'] != 'open':
                raise HubError('interactive_review_completed', 409)
            manifest = {entry['id']: entry for entry in item['items']}
            changes = []
            at = datetime.now(timezone.utc).isoformat()
            for answer in normalized:
                entry = manifest.get(answer['item_id'])
                if entry is None:
                    raise HubError('review_item_not_found', 404)
                if 'choices' in entry and answer['value'] not in {choice['id'] for choice in entry['choices']}:
                    raise HubError('invalid_review_choice')
                old = item['answers'].get(answer['item_id'])
                if old and old['value'] == answer['value'] and old['by'] == actor:
                    continue
                item['answers'][answer['item_id']] = {'value': answer['value'], 'by': actor, 'at': at}
                changes.append(answer)
            if complete and len(item['answers']) != len(item['items']):
                raise HubError('review_answers_incomplete', 409)
            if changes or complete:
                item['revision'] += 1
                item['state'] = 'completed' if complete else 'open'
                self._save(db, actor, 'interactive_review.completed' if complete else 'interactive_review.answered', item)
                for answer in changes:
                    db.execute('INSERT INTO hub_interactive_review_answer_events VALUES(?,?,?,?,?,?)',
                        (item['id'], item['revision'], answer['item_id'], actor, answer['value'], at))
            db.execute('INSERT INTO hub_interactive_review_requests VALUES(?,?,?,?)', (actor, key, fingerprint, item['id']))
            return item
