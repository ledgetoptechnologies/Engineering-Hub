"""Project-scoped durable audit history, not a timeline inferred from current state."""
import re

from fastapi import HTTPException, Query, Request

from .repository import HubError, HubRepository, PROJECTS


def project_activity(repository, *, project, limit=50, before=None):
    if type(repository) is not HubRepository:
        raise ValueError('Explicit Hub repository required')
    if project not in PROJECTS:
        raise HubError('unknown_project', 404)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise HubError('invalid_limit', 422)
    if before is not None and (type(before) is not int or not 1 <= before <= 9223372036854775807):
        raise HubError('invalid_activity_cursor', 422)
    # One snapshot ties every audit record to its persisted project. Neither
    # unscoped policy records nor deleted/unresolvable entities are exposed.
    with repository._connection() as db:
        db.execute('BEGIN')
        rows = db.execute('''
            SELECT a.sequence,a.actor,a.action,a.entity_id,a.revision,a.created_at,
                   e.kind,json_extract(e.data,'$.title') AS title
            FROM hub_audit a JOIN hub_entities e ON e.id=a.entity_id
            WHERE e.kind IN ('task','decision','run')
              AND json_extract(e.data,'$.project')=?
              AND a.action GLOB e.kind || '.[a-z]*'
              AND (? IS NULL OR a.sequence < ?)
            ORDER BY a.sequence DESC LIMIT ?
        ''', (project, before, before, limit + 1)).fetchall()
    items = []
    for row in rows[:limit]:
        title = row['title']
        # No raw entity JSON, candidate configuration, payloads or credentials.
        item = {key: row[key] for key in ('sequence', 'action', 'entity_id', 'revision', 'created_at', 'kind')}
        item['actor'] = row['actor'] if re.fullmatch(r'[A-Za-z0-9@_.:+-]{1,200}', row['actor']) else 'unknown'
        item['current_title'] = title[:200] if isinstance(title, str) else None
        items.append(item)
    return {'project': project, 'source': 'hub_audit', 'items': items,
            'next_before': items[-1]['sequence'] if len(rows) > limit and items else None,
            'title_semantics': 'current_entity_title_not_historical',
            'coverage': ['tasks', 'decisions', 'runs']}


def attach_activity_routes(app, *, repository, actor):
    @app.get('/api/hub/v1/activity')
    def activity(request: Request, project: str = Query(min_length=1, max_length=64),
                 limit: int = Query(default=50, ge=1, le=100),
                 before: int | None = Query(default=None, ge=1, le=9223372036854775807)):
        actor(request)
        try:
            return project_activity(repository, project=project, limit=limit, before=before)
        except HubError:
            raise
        except Exception:
            raise HTTPException(503, detail={'code': 'activity_unavailable'}) from None
