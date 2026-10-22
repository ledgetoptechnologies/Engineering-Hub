"""Advisory plans and ideas in the existing project-shared knowledge store.

Entries are data, never instructions, approvals, task claims or execution rights.
Namespaces organize records; they do not add participant-level access controls.
"""
from __future__ import annotations

import json
import re

from agent_message_bus.knowledge import (KnowledgeError, KnowledgeIntegrityError,
                                         entry_dict)

SCHEMA = 'engineering-hub.planning.v1'
KINDS = ('plan', 'feature', 'idea', 'improvement')
STATUSES = ('proposed', 'planned', 'in_progress', 'done', 'declined')
_TOPIC = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
_ITEM = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
_FIELDS = {'schema', 'scope', 'project', 'topic', 'kind', 'item_id', 'title', 'content', 'status'}


class PlanningError(KnowledgeError):
    def __init__(self):
        super().__init__('Invalid planning item')


class PlanningIntegrityError(KnowledgeIntegrityError):
    def __init__(self):
        super().__init__('Planning item integrity failure')


def _prefix(repo, *, scope, project, topic=None):
    repo.validate_project(project)
    if scope == 'topic':
        if type(topic) is not str or not _TOPIC.fullmatch(topic):
            raise PlanningError()
        return f'planning/topics/{topic}'
    if scope == 'hub_improvements' and project == 'engineering-hub' and topic is None:
        return 'planning/hub-improvements'
    raise PlanningError()


def item_path(repo, *, scope, project, topic=None, kind, item_id):
    prefix = _prefix(repo, scope=scope, project=project, topic=topic)
    if (type(kind) is not str or kind not in KINDS
            or type(item_id) is not str or not _ITEM.fullmatch(item_id)):
        raise PlanningError()
    return f'{prefix}/{kind}/{item_id}.json'


def _document(*, scope, project, topic, kind, item_id, title, content, status):
    if (type(title) is not str or not title.strip() or len(title) > 200
            or type(content) is not str or '\x00' in content
            or type(status) is not str or status not in STATUSES):
        raise PlanningError()
    if any(ord(char) < 32 and char not in '\t\n\r' or ord(char) == 127
           for char in title + content):
        raise PlanningError()
    try:
        if len(content.encode('utf-8')) > 48 * 1024:
            raise PlanningError()
        title.encode('utf-8')
    except UnicodeEncodeError:
        raise PlanningError() from None
    return dict(schema=SCHEMA, scope=scope, project=project, topic=topic, kind=kind,
                item_id=item_id, title=title, content=content, status=status)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PlanningIntegrityError()
        result[key] = value
    return result


def _decode(repo, entry, *, include_content=True):
    try:
        data = json.loads(entry.content, object_pairs_hook=_unique)
        if type(data) is not dict or set(data) != _FIELDS or data['schema'] != SCHEMA:
            raise PlanningIntegrityError()
        path = item_path(repo, scope=data['scope'], project=data['project'], topic=data['topic'],
                         kind=data['kind'], item_id=data['item_id'])
        checked = _document(**{key: value for key, value in data.items() if key != 'schema'})
        if entry.project != data['project'] or entry.path != path or entry.title != data['title']:
            raise PlanningIntegrityError()
        result = entry_dict(entry, include_content=False)
        result.update({key: value for key, value in checked.items() if key != 'schema'})
        result.update(advisory=True, execution_authorization=False)
        if not include_content:
            result.pop('content')
        return result
    except (ValueError, TypeError, KeyError, RecursionError):
        raise PlanningIntegrityError() from None


def put_item(repo, *, author, scope, project, topic=None, kind, item_id,
             title, content, status, expected_version):
    path = item_path(repo, scope=scope, project=project, topic=topic, kind=kind, item_id=item_id)
    document = _document(scope=scope, project=project, topic=topic, kind=kind, item_id=item_id,
                         title=title, content=content, status=status)
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    entry = repo.put(project=project, path=path, title=title, content=raw,
                     author=author, expected_version=expected_version)
    return _decode(repo, entry)


def get_item(repo, *, scope, project, topic=None, kind, item_id):
    path = item_path(repo, scope=scope, project=project, topic=topic, kind=kind, item_id=item_id)
    return _decode(repo, repo.get(project, path))


def list_items(repo, *, scope, project, topic=None, kind=None, limit=50):
    prefix = _prefix(repo, scope=scope, project=project, topic=topic)
    if kind is not None:
        if type(kind) is not str or kind not in KINDS:
            raise PlanningError()
        prefix += '/' + kind
    if type(limit) is not int or not 1 <= limit <= 99:
        raise PlanningError()
    entries = repo.list(project=project, path_prefix=prefix, limit=limit + 1)
    return {'items': [_decode(repo, entry, include_content=False) for entry in entries[:limit]],
            'truncated': len(entries) > limit, 'advisory': True, 'execution_authorization': False}
