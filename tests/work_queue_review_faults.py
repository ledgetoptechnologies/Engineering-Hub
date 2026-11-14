"""Fault injection for stale review data that normal active-work APIs forbid."""
import json

import pytest

from engineering_hub.repository import HubError


def corrupt_review_after_active_work(repo, task_id):
    task = repo.get_task(task_id)
    with pytest.raises(HubError, match='active_work_job'):
        repo.set_candidate('codex', task['id'], task['revision'], 'c' * 40)
    # Simulate retained legacy/corrupt data only; this is not a supported API.
    with repo._transaction() as db:
        item = repo._get(db, task_id, 'task')
        item['review']['verdict'] = 'reject'
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(item), task_id))
