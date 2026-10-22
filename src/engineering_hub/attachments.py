"""Bounded, inert task/decision images in the existing owned Hub ledger.

Images supplement context; they never answer a decision or authorize execution.
No paths, remote fetches or filesystem writes are accepted by this repository.
Transport authentication and human CSRF remain the application's responsibility.
"""
from contextlib import contextmanager
import hashlib
import uuid

from .label_sets import _image, MAX_TOTAL_BYTES
from .repository import HubError, HubRepository

MAX_IMAGES_PER_ENTITY = 8


def _uuid(value):
    try:
        if type(value) is str and str(uuid.UUID(value)) == value:
            return value
    except ValueError:
        pass
    raise HubError('invalid_attachment_id', 422)


def _kind(value):
    if type(value) is not str or value not in {'task', 'decision'}:
        raise HubError('invalid_attachment_kind', 422)
    return value


class EntityImageRepository:
    def __init__(self, repo):
        if type(repo) is not HubRepository:
            raise TypeError('HubRepository required')
        self.repo = repo
        with repo._transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS hub_entity_images (
                id TEXT PRIMARY KEY, entity_kind TEXT NOT NULL, entity_id TEXT NOT NULL,
                data BLOB NOT NULL, media_type TEXT NOT NULL, source_sha256 TEXT NOT NULL,
                title TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL,
                entity_revision INTEGER NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS hub_entity_images_entity '
                       'ON hub_entity_images(entity_kind,entity_id)')

    @contextmanager
    def _read(self):
        with self.repo._connection() as db:
            db.execute('BEGIN')
            try:
                yield db
            finally:
                db.rollback()

    @staticmethod
    def _public(row, project):
        return {'id': row['id'], 'kind': row['entity_kind'], 'entity_id': row['entity_id'],
                'project': project, 'title': row['title'], 'media_type': row['media_type'],
                'bytes': len(row['data']), 'sha256': hashlib.sha256(row['data']).hexdigest(),
                'by': row['actor'], 'created_at': row['created_at'],
                'entity_revision': row['entity_revision'],
                'url': '/api/hub/v1/attachments/' + row['id'] + '/image'}

    def _target(self, db, kind, entity_id, *, require_active=False):
        item = self.repo._get(db, _uuid(entity_id), _kind(kind))
        self.repo._project(item['project'], db, require_active=require_active)
        return item

    def put(self, *, image_id, kind, entity_id, expected_revision, content,
            media_type, title='', actor):
        self.repo._actor(actor)
        _uuid(image_id)
        _kind(kind)
        _uuid(entity_id)
        if (type(expected_revision) is not int or expected_revision < 1
                or type(title) is not str or len(title) > 200
                or any(ord(char) < 32 or ord(char) == 127 for char in title)):
            raise HubError('invalid_attachment', 422)
        clean = _image(content, media_type)
        source_hash = hashlib.sha256(content).hexdigest()
        with self.repo._transaction() as db:
            target = self._target(db, kind, entity_id, require_active=True)
            previous = db.execute('SELECT * FROM hub_entity_images WHERE id=?', (image_id,)).fetchone()
            if previous is not None:
                if (previous['entity_kind'] != kind or previous['entity_id'] != entity_id
                        or previous['actor'] != actor or previous['title'] != title
                        or previous['media_type'] != media_type or previous['source_sha256'] != source_hash):
                    raise HubError('attachment_id_conflict', 409)
                # A lost response may be replayed after the decision closes or
                # task advances. It cannot add bytes or repeat a revision.
                return self._public(previous, target['project'])
            self.repo._cas(target, expected_revision)
            if kind == 'decision':
                if target['status'] != 'open':
                    raise HubError('decision_closed', 409)
            else:
                if self.repo.queued_pilot_enrolled(db):
                    raise HubError('queued_pilot_admission_required', 409)
                self.repo._no_active_run(db, entity_id)
                if (target['status'] not in {'assigned', 'claimed'}
                        or target.get('candidate_commit') is not None or target.get('review') is not None):
                    raise HubError('task_context_locked', 409)
                if (not actor.startswith('human:') and actor != target.get('owner')
                        and not (target.get('owner') is None and actor == target.get('proposed_by'))):
                    raise HubError('forbidden', 403)
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_work_jobs'").fetchone():
                    active = db.execute('''SELECT 1 FROM hub_work_jobs
                        WHERE json_extract(target,'$.task_id')=?
                          AND state NOT IN ('succeeded','failed','cancelled') LIMIT 1''', (entity_id,)).fetchone()
                    if active:
                        raise HubError('task_context_locked', 409)
            if db.execute('SELECT count(*) FROM hub_entity_images WHERE entity_kind=? AND entity_id=?',
                          (kind, entity_id)).fetchone()[0] >= MAX_IMAGES_PER_ENTITY:
                raise HubError('attachment_quota', 413)
            total = db.execute('SELECT coalesce(sum(length(data)),0) FROM hub_entity_images').fetchone()[0]
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_label_images'").fetchone():
                total += db.execute('SELECT coalesce(sum(length(data)),0) FROM hub_label_images').fetchone()[0]
            if total + len(clean) > MAX_TOTAL_BYTES:
                raise HubError('attachment_quota', 413)
            saved = self.repo._save(db, actor, kind + '.attachment_added', kind, target)
            db.execute('INSERT INTO hub_entity_images VALUES(?,?,?,?,?,?,?,?,?,?)', (
                image_id, kind, entity_id, clean, media_type, source_hash, title,
                actor, saved['updated_at'], saved['revision']))
            row = db.execute('SELECT * FROM hub_entity_images WHERE id=?', (image_id,)).fetchone()
            return self._public(row, target['project'])

    def list(self, *, kind, entity_id, actor):
        self.repo._actor(actor)
        with self._read() as db:
            target = self._target(db, kind, entity_id)
            rows = db.execute('SELECT * FROM hub_entity_images WHERE entity_kind=? AND entity_id=? '
                              'ORDER BY entity_revision,id', (kind, entity_id)).fetchall()
            return {'project': target['project'], 'entity_revision': target['revision'],
                    'items': [self._public(row, target['project']) for row in rows]}

    def image(self, *, image_id, actor):
        self.repo._actor(actor)
        with self._read() as db:
            row = db.execute('SELECT * FROM hub_entity_images WHERE id=?', (_uuid(image_id),)).fetchone()
            if row is None:
                raise HubError('attachment_not_found', 404)
            self._target(db, row['entity_kind'], row['entity_id'])
            return bytes(row['data']), row['media_type']
