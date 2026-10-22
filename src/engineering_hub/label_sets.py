"""Bounded data-only label sets in the existing, owned Hub ledger.

Transport authentication/CSRF belongs to the app. Actor checks here are a second
boundary: agents supply material, only authenticated humans assign labels.
"""
from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
from io import BytesIO
import json
import math
import re
import threading
import warnings

from .repository import HubError, HubRepository, _json


MAX_IMAGE_BYTES = 256 * 1024
MAX_SET_BYTES = 25 * 1024 * 1024
MAX_TOTAL_BYTES = 250 * 1024 * 1024
MAX_SETS_PER_PROJECT = 100
MAX_ITEMS_GLOBAL = 50_000
MAX_PIXELS = 16_000_000
_SLUG = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
_META = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,31}')
_RESERVED = frozenset({'__proto__', 'prototype', 'constructor'})
_DECODER = threading.BoundedSemaphore(1)


def _id(value):
    if (not isinstance(value, str) or _SLUG.fullmatch(value) is None
            or '..' in value):
        raise HubError('invalid_label_set', 422)
    return value


def _text(value, maximum, empty=False):
    if (not isinstance(value, str) or len(value) > maximum
            or (not empty and not value.strip())
            or any(ord(char) < 32 and char not in '\n\r\t' for char in value)):
        raise HubError('invalid_label_set', 422)
    return value


def _image(content, content_type):
    # A per-image bound alone does not bound concurrent decode memory. Reject
    # concurrent decoders instead of accumulating work in the main Hub process.
    if not _DECODER.acquire(blocking=False):
        raise HubError('image_decoder_busy', 429)
    try:
        return _decode_image(content, content_type)
    finally:
        _DECODER.release()


def _decode_image(content, content_type):
    """Decode fully then re-encode, discarding metadata and trailing payloads."""
    if (type(content) is not bytes or not content or len(content) > MAX_IMAGE_BYTES
            or content_type not in {'image/jpeg', 'image/png'}):
        raise HubError('invalid_label_image', 422)
    from PIL import Image, UnidentifiedImageError
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as source:
                expected = 'JPEG' if content_type == 'image/jpeg' else 'PNG'
                if (source.format != expected or source.width * source.height > MAX_PIXELS
                        or getattr(source, 'n_frames', 1) != 1):
                    raise ValueError()
                source.verify()
            with Image.open(BytesIO(content)) as source:
                source.load()
                clean = source.convert('RGB' if expected == 'JPEG' else 'RGBA')
                with clean:
                    output = BytesIO()
                    clean.save(output, format=expected)
                    result = output.getvalue()
                if len(result) > MAX_IMAGE_BYTES:
                    raise ValueError()
                return result
    except (ValueError, OSError, SyntaxError, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HubError('invalid_label_image', 422) from None


class LabelSetRepository:
    """Explicit composition only; never accepts an arbitrary SQLite path."""
    def __init__(self, repo):
        if type(repo) is not HubRepository:
            raise TypeError('HubRepository required')
        self.repo = repo
        with repo._transaction() as db:
            for statement in (
                'CREATE TABLE IF NOT EXISTS hub_label_sets (id TEXT PRIMARY KEY, '
                'project TEXT NOT NULL, data TEXT NOT NULL, fingerprint TEXT NOT NULL, '
                'revision INTEGER NOT NULL, updated_at TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS hub_label_items (set_id TEXT NOT NULL, '
                'id TEXT NOT NULL, position INTEGER NOT NULL, meta TEXT NOT NULL, '
                'label TEXT, revision INTEGER NOT NULL, PRIMARY KEY(set_id,id))',
                'CREATE TABLE IF NOT EXISTS hub_label_images (set_id TEXT NOT NULL, '
                'item_id TEXT NOT NULL, media_type TEXT NOT NULL, data BLOB NOT NULL, '
                'source_sha256 TEXT NOT NULL, PRIMARY KEY(set_id,item_id))',
                'CREATE INDEX IF NOT EXISTS hub_label_sets_project ON hub_label_sets(project)',
            ):
                db.execute(statement)

    @contextmanager
    def _read(self):
        with self.repo._connection() as db:
            db.execute('BEGIN')
            try:
                yield db
            finally:
                db.rollback()

    def _record(self, db, actor, action, set_id):
        now = datetime.now(timezone.utc).isoformat()
        db.execute('UPDATE hub_label_sets SET revision=revision+1, updated_at=? WHERE id=?', (now, set_id))
        revision = db.execute('SELECT revision FROM hub_label_sets WHERE id=?', (set_id,)).fetchone()[0]
        db.execute('INSERT INTO hub_audit(actor,action,entity_id,revision,created_at) VALUES(?,?,?,?,?)',
                   (actor, action, set_id, revision, now))
        self.repo._emit(db, action, {'id': set_id, 'revision': revision, 'updated_at': now})

    @staticmethod
    def _set(db, set_id):
        row = db.execute('SELECT * FROM hub_label_sets WHERE id=?', (_id(set_id),)).fetchone()
        if row is None:
            raise HubError('label_set_not_found', 404)
        return row

    @staticmethod
    def _item(db, set_id, item_id):
        row = db.execute('SELECT * FROM hub_label_items WHERE set_id=? AND id=?',
                         (set_id, _id(item_id))).fetchone()
        if row is None:
            raise HubError('label_item_not_found', 404)
        return row

    @staticmethod
    def _public_item(row):
        return {'id': row['id'], 'meta': json.loads(row['meta']),
                'label': json.loads(row['label']) if row['label'] is not None else None,
                'revision': row['revision']}

    def _get(self, db, set_id):
        row = self._set(db, set_id)
        result = json.loads(row['data'])
        result['updated_at'] = row['updated_at']
        result['items'] = [self._public_item(item) for item in db.execute(
            'SELECT * FROM hub_label_items WHERE set_id=? ORDER BY position', (set_id,))]
        return result

    def create(self, *, set_id, project, title, instructions='', choices, items, actor):
        self.repo._actor(actor, agent=True)
        self.repo._project(project)
        _id(set_id)
        _text(title, 200)
        _text(instructions, 4096, empty=True)
        if type(choices) is not list or not 2 <= len(choices) <= 6:
            raise HubError('invalid_label_set', 422)
        normalized_choices = []
        for choice in choices:
            if type(choice) is not dict or set(choice) != {'key', 'label'}:
                raise HubError('invalid_label_set', 422)
            normalized_choices.append({'key': _id(choice['key']), 'label': _text(choice['label'], 80)})
        if len({choice['key'] for choice in normalized_choices}) != len(choices):
            raise HubError('invalid_label_set', 422)
        if type(items) is not list or not 1 <= len(items) <= 1000:
            raise HubError('invalid_label_set', 422)
        normalized_items = []
        for item in items:
            if type(item) is not dict or set(item) - {'id', 'meta'} or 'id' not in item:
                raise HubError('invalid_label_set', 422)
            meta = item.get('meta', {})
            if type(meta) is not dict or len(meta) > 8:
                raise HubError('invalid_label_set', 422)
            for key, value in meta.items():
                if not isinstance(key, str) or _META.fullmatch(key) is None or key in _RESERVED:
                    raise HubError('invalid_label_set', 422)
                if isinstance(value, str):
                    _text(value, 120, empty=True)
                elif (type(value) not in {int, float} or abs(value) > 1.7976931348623157e308
                      or not math.isfinite(value)):
                    raise HubError('invalid_label_set', 422)
            normalized_items.append({'id': _id(item['id']), 'meta': meta})
        if len({item['id'] for item in normalized_items}) != len(items):
            raise HubError('invalid_label_set', 422)
        data = {'id': set_id, 'project': project, 'title': title,
                'instructions': instructions, 'choices': normalized_choices}
        fingerprint = hashlib.sha256(_json({'set': data, 'items': normalized_items}).encode()).hexdigest()
        with self.repo._transaction() as db:
            existing = db.execute('SELECT fingerprint FROM hub_label_sets WHERE id=?', (set_id,)).fetchone()
            if existing is not None:
                if existing[0] != fingerprint:
                    raise HubError('label_set_conflict', 409)
                return self._get(db, set_id)
            # Match the UI's bounded project listing; never silently truncate.
            if db.execute('SELECT count(*) FROM hub_label_sets WHERE project=?', (project,)).fetchone()[0] >= MAX_SETS_PER_PROJECT:
                raise HubError('label_set_limit', 409)
            if db.execute('SELECT count(*) FROM hub_label_items').fetchone()[0] + len(items) > MAX_ITEMS_GLOBAL:
                raise HubError('label_set_limit', 409)
            db.execute('INSERT INTO hub_label_sets VALUES(?,?,?,?,?,?)',
                       (set_id, project, _json(data), fingerprint, 0, ''))
            for position, item in enumerate(normalized_items):
                db.execute('INSERT INTO hub_label_items VALUES(?,?,?,?,?,?)',
                           (set_id, item['id'], position, _json(item['meta']), None, 0))
            self._record(db, actor, 'label_set.created', set_id)
            return self._get(db, set_id)

    def get(self, set_id):
        with self._read() as db:
            return self._get(db, set_id)

    def list(self, project):
        self.repo._project(project)
        with self._read() as db:
            items = []
            for row in db.execute('SELECT * FROM hub_label_sets WHERE project=? ORDER BY updated_at DESC,id', (project,)):
                counts = db.execute('SELECT count(*),count(label) FROM hub_label_items WHERE set_id=?', (row['id'],)).fetchone()
                items.append({'id': row['id'], 'project': project, 'title': json.loads(row['data'])['title'],
                              'item_count': counts[0], 'labeled_count': counts[1], 'updated_at': row['updated_at']})
            return {'items': items}

    def results(self, set_id):
        # Full data-only snapshot, no blobs/paths or agent-authored executable data.
        return self.get(set_id)

    def upload_image(self, set_id, item_id, *, content, content_type, actor):
        self.repo._actor(actor, agent=True)
        _id(set_id)
        _id(item_id)
        clean = _image(content, content_type)
        digest = hashlib.sha256(content).hexdigest()
        with self.repo._transaction() as db:
            self._set(db, set_id)
            self._item(db, set_id, item_id)
            old = db.execute('SELECT source_sha256,media_type FROM hub_label_images WHERE set_id=? AND item_id=?',
                             (set_id, item_id)).fetchone()
            if old is not None:
                if old[0] != digest or old[1] != content_type:
                    raise HubError('label_image_conflict', 409)
            else:
                used = db.execute('SELECT coalesce(sum(length(data)),0) FROM hub_label_images WHERE set_id=?', (set_id,)).fetchone()[0]
                if used + len(clean) > MAX_SET_BYTES:
                    raise HubError('label_image_quota', 413)
                total = db.execute('SELECT coalesce(sum(length(data)),0) FROM hub_label_images').fetchone()[0]
                # Both bounded raster modules share this single ledger budget.
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_entity_images'").fetchone():
                    total += db.execute('SELECT coalesce(sum(length(data)),0) FROM hub_entity_images').fetchone()[0]
                if total + len(clean) > MAX_TOTAL_BYTES:
                    raise HubError('label_image_quota', 413)
                db.execute('INSERT INTO hub_label_images VALUES(?,?,?,?,?)',
                           (set_id, item_id, content_type, clean, digest))
                self._record(db, actor, 'label_image.uploaded', set_id)
            return {'set_id': set_id, 'item_id': item_id, 'content_type': content_type,
                    'sha256': hashlib.sha256(clean).hexdigest(), 'bytes': len(clean)}

    def image(self, set_id, item_id):
        with self._read() as db:
            self._set(db, set_id)
            self._item(db, set_id, item_id)
            row = db.execute('SELECT data,media_type FROM hub_label_images WHERE set_id=? AND item_id=?',
                             (set_id, item_id)).fetchone()
            if row is None:
                raise HubError('label_image_not_found', 404)
            return bytes(row[0]), row[1]

    def label(self, set_id, item_id, *, choice, expected_revision, actor):
        self.repo._actor(actor, human=True)
        with self.repo._transaction() as db:
            row = self._set(db, set_id)
            item = self._item(db, set_id, item_id)
            if type(expected_revision) is not int or expected_revision != item['revision']:
                raise HubError('label_conflict', 409)
            if choice is not None and (not isinstance(choice, str) or choice not in {
                    value['key'] for value in json.loads(row['data'])['choices']}):
                raise HubError('invalid_choice', 422)
            if choice is not None and db.execute('SELECT 1 FROM hub_label_images WHERE set_id=? AND item_id=?',
                                                (set_id, item_id)).fetchone() is None:
                raise HubError('label_image_required', 409)
            label = None if choice is None else _json({'choice': choice, 'by': actor,
                                                       'at': datetime.now(timezone.utc).isoformat()})
            db.execute('UPDATE hub_label_items SET label=?,revision=revision+1 WHERE set_id=? AND id=?',
                       (label, set_id, item_id))
            self._record(db, actor, 'label_item.labeled', set_id)
            return self._public_item(self._item(db, set_id, item_id))
