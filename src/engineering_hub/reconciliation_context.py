"""Local write guard for one admitted staging reconciliation.

This guards local ledger consistency, not remote effects or a host fence.
"""
import asyncio
import json
from contextlib import contextmanager
from contextvars import ContextVar

current = ContextVar('hub_staging_reconciliation_guard', default=None)


class Changed(Exception):
    """The admitted local state changed outside the current operation."""


class Guard:
    def __init__(self, path, submission_id, job_id, project, resource_ids, max_resources):
        self.path = str(path)
        self.submission_id = submission_id
        self.job_id = job_id
        self.project = project
        self.resource_ids = tuple(sorted(resource_ids))
        self.max_resources = max_resources
        self.expected = None
        self.admission_id = None
        self.task = asyncio.current_task()
        self.active = True

    @staticmethod
    def _row(db, sql, params):
        row = db.execute(sql, params).fetchone()
        return tuple(row) if row is not None else None

    def snapshot(self, db):
        item = self._row(db, 'SELECT * FROM hub_staging_submissions WHERE id=?', (self.submission_id,))
        job = self._row(db, 'SELECT * FROM hub_work_jobs WHERE id=?', (self.job_id,))
        if item is None or job is None:
            raise Changed()
        try:
            data = json.loads(db.execute('SELECT data FROM hub_staging_submissions WHERE id=?', (self.submission_id,)).fetchone()[0])
            if not isinstance(data, dict):
                raise ValueError('invalid submission')
        except (ValueError, TypeError):
            raise Changed() from None
        task_id = data.get('review_task_id')
        task = self._row(db, "SELECT * FROM hub_entities WHERE id=? AND kind='task'", (task_id,)) if task_id else None
        pause = self._row(db, "SELECT * FROM hub_entities WHERE id='pause' AND kind='policy'", ())
        receipt = self._row(db, 'SELECT * FROM hub_staging_reconcile_admissions WHERE id=?', (self.admission_id,)) if self.admission_id else None
        if self.admission_id and receipt is None:
            raise Changed()
        machine_id = db.execute('SELECT machine_id FROM hub_work_jobs WHERE id=?', (self.job_id,)).fetchone()[0]
        machine = self._row(db, 'SELECT * FROM hub_work_machines WHERE machine_id=?', (machine_id,))
        ownership = db.execute('SELECT * FROM hub_staging_resource_ownership WHERE job_id=? ORDER BY id LIMIT ?',
                               (self.job_id, self.max_resources + 1)).fetchall()
        if len(ownership) > self.max_resources:
            raise Changed()
        bad = db.execute("SELECT id FROM hub_entities WHERE kind='staging_resource' AND (CASE WHEN json_valid(data) THEN CASE WHEN json_type(data,'$.job_id')='text' THEN 0 ELSE 1 END ELSE 1 END)=1 LIMIT 1").fetchone()
        if bad is not None:
            raise Changed()
        references = db.execute("SELECT id FROM hub_entities WHERE kind='staging_resource' AND (CASE WHEN json_valid(data) THEN json_extract(data,'$.job_id') END)=? ORDER BY id LIMIT ?",
                                (self.job_id, self.max_resources + 1)).fetchall()
        if len(references) > self.max_resources:
            raise Changed()
        owner_ids = {row['id'] for row in ownership}
        if any(row['id'] not in owner_ids for row in references):
            raise Changed()
        identities = sorted(set(self.resource_ids) | owner_ids | {row['id'] for row in references})
        resources = tuple((identity,
            self._row(db, "SELECT * FROM hub_entities WHERE id=? AND kind='staging_resource'", (identity,)),
            self._row(db, 'SELECT * FROM hub_staging_resource_ownership WHERE id=?', (identity,)))
            for identity in identities)
        resource_job = self._row(db, 'SELECT * FROM hub_staging_resource_jobs WHERE id=?', (self.job_id,))
        staging_policy = self._row(db, 'SELECT * FROM hub_staging_policy WHERE project=?', (self.project,))
        work_policy = self._row(db, 'SELECT * FROM hub_work_policy WHERE id=1', ())
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_projects'").fetchone()
        project = self._row(db, 'SELECT * FROM hub_projects WHERE id=?', (self.project,)) if exists else None
        return item, job, machine, tuple(tuple(row) for row in ownership), resources, resource_job, staging_policy, work_policy, project, bool(exists), task, pause, receipt

    def _owner(self, path):
        if not self.active or str(path) != self.path or asyncio.current_task() is not self.task:
            raise Changed()

    def before_write(self, path, db):
        self._owner(path)
        if self.expected is None or self.snapshot(db) != self.expected:
            raise Changed()

    def prepare_commit(self, path, db):
        self._owner(path)
        return self.snapshot(db)

    def committed(self, state):
        self.expected = state


@contextmanager
def suspended():
    token = current.set(None)
    try:
        yield
    finally:
        current.reset(token)
