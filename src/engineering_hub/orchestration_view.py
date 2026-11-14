"""Bounded, owner-only, read-only request view for the human dashboard."""
import json

from .dispatch import DispatchRepository, _canonical_id
from .repository import HubError
from .task_progress import _key


def request_view(repo, actor, project, request_id, limit, after):
    repo._actor(actor, human=True)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise HubError('invalid_limit', 422)
    if after is not None:
        _canonical_id(after)
    with repo._connection() as db:
        db.execute('BEGIN')
        request = DispatchRepository(repo)._request(db, request_id)
        if request['project'] != project:
            raise HubError('dispatch_unavailable', 404)
        rows = db.execute("SELECT d.data AS dispatch, t.kind AS task_kind, t.data AS task, "
                          "q.data AS decision FROM hub_entities d "
                          "LEFT JOIN hub_entities t ON t.id=json_extract(d.data,'$.task_id') "
                          "LEFT JOIN hub_entities q ON q.id=json_extract(d.data,'$.decision_id') AND q.kind='decision' "
                          "WHERE d.kind='role_dispatch' AND json_extract(d.data,'$.request_id')=? "
                          "AND d.id>? ORDER BY d.id LIMIT ?", (request['id'], after or '', limit + 1)).fetchall()
        visible = rows[:limit]
        dispatches = [json.loads(row['dispatch']) for row in visible]
        keys = [_key(item['task_id']) for item in dispatches]
        progress = {}
        if keys:
            marks = ','.join('?' for _ in keys)
            for row in db.execute(f"SELECT id,data FROM hub_entities WHERE kind='task_progress' AND id IN ({marks})", keys):
                progress[row['id']] = json.loads(row['data'])
        items = []
        for row, dispatch in zip(visible, dispatches):
            task = json.loads(row['task']) if row['task'] else None
            if (dispatch.get('project') != project or task is None or task.get('project') != project
                    or row['task_kind'] != dispatch.get('task_kind') or row['task_kind'] not in ('task', 'tracking_task')):
                raise HubError('dispatch_unavailable', 404)
            p = progress.get(_key(dispatch['task_id']))
            if p and (p.get('project') != project or p.get('task_id') != dispatch['task_id'] or p.get('task_kind') != row['task_kind']):
                raise HubError('dispatch_unavailable', 404)
            decision = json.loads(row['decision']) if row['decision'] else None
            if dispatch.get('decision_id') and (not decision or decision.get('project') != project
                    or decision.get('origin_dispatch_id') != dispatch['id']):
                raise HubError('dispatch_unavailable', 404)
            pending = None
            if decision and decision.get('status') == 'open':
                pending = dict(id=decision['id'], title=decision['title'], options=decision['options'], updated_at=decision['updated_at'])
            items.append(dict(id=dispatch['id'], task_id=dispatch['task_id'], task_title=task['title'],
                task_status=task['status'], task_completion_verified=task['status'] in ('verified', 'completion_verified'),
                role=dispatch['role_id'], state=dispatch['state'], updated_at=dispatch['updated_at'],
                stale_checkpoint=dispatch['coordination_stale'], report=dispatch['report'],
                dispatch_completion_verified=dispatch['completion_verified'], pending_decision=pending,
                progress=None if p is None else dict(checklist=p['checklist'], percent=p['percent'],
                    eta_seconds=p['eta_seconds'], agent_eta_guess=p['agent_eta_guess'],
                    status_line=p['status_line'], blocked_on=p['blocked_on'], updated_at=p['updated_at'])))
        return dict(project=project, request=dict(id=request['id'], title=request['title'], updated_at=request['updated_at']),
            dispatches=items, next_after=dispatches[-1]['id'] if len(rows) > limit else None,
            snapshot='single_read_transaction', freshness='checkpoint_only', execution_authorization=False)
