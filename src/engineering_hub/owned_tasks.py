"""Explicit in-process ownership of accepted pilot work; no autonomous workers."""
import asyncio
import inspect
import uuid

from .repository import HubError
from .lifecycle import finish_owned


class OwnedPilotTasks:
    """Deduplicate only concurrent work, keeping it alive after caller detach."""

    def __init__(self, *, max_inflight=16):
        if type(max_inflight) is not int or not 1 <= max_inflight <= 64:
            raise HubError('invalid_pilot_task_limit')
        self._limit = max_inflight
        self._tasks = {}
        self._state = 'not_started'
        self._loop = None

    @property
    def status(self):
        return {'state': self._state, 'inflight': len(self._tasks),
                'max_inflight': self._limit}

    async def start(self):
        if self._state != 'not_started':
            raise HubError('pilot_tasks_already_started', 409)
        self._loop = asyncio.get_running_loop()
        self._state = 'active'
        return self.status

    def _check_loop(self):
        if self._loop is not asyncio.get_running_loop():
            raise HubError('pilot_tasks_unavailable', 503)

    def _finished(self, key, task):
        if self._tasks.get(key) is task:
            del self._tasks[key]
        if not task.cancelled():
            task.exception()  # Retrieve detached failures without logging them.

    async def run(self, kind, run_id, factory):
        if type(kind) is not str or kind not in {'advance', 'recover'}:
            raise HubError('invalid_pilot_task_kind')
        try:
            valid_id = type(run_id) is str and str(uuid.UUID(run_id)) == run_id
        except (ValueError, AttributeError):
            valid_id = False
        if not valid_id or not callable(factory):
            raise HubError('invalid_pilot_task_request')
        if self._state != 'active':
            raise HubError('pilot_tasks_unavailable', 503)
        self._check_loop()
        key = (kind, run_id)
        task = self._tasks.get(key)
        if task is not None and task.done():
            self._finished(key, task)
            task = None
        if task is None:
            if len(self._tasks) >= self._limit:
                raise HubError('pilot_tasks_capacity', 503)

            async def invoke():
                awaitable = factory()
                if not inspect.isawaitable(awaitable):
                    raise HubError('invalid_pilot_task_factory')
                return await awaitable

            task = asyncio.create_task(invoke())
            self._tasks[key] = task
            task.add_done_callback(lambda done: self._finished(key, done))
        return await asyncio.shield(task)

    async def stop(self):
        if self._state == 'not_started':
            self._state = 'stopped'
            return self.status
        self._check_loop()
        self._state = 'stopping'
        accepted = tuple(self._tasks.values())
        try:
            await finish_owned(asyncio.gather(*accepted, return_exceptions=True))
        finally:
            # finish_owned returns/raises only once all accepted work is terminal.
            self._state = 'stopped'
        return self.status
