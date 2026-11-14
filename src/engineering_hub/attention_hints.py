"""Bounded in-process exact-session invalidation hints, never a wake signal."""
import asyncio
import threading
import uuid

from .repository import HubError


class AttentionHintBroker:
    def __init__(self, maximum=64):
        self.maximum = maximum
        self._lock = threading.Lock()
        self._drained = threading.Condition(self._lock)
        self._listeners = {}
        self._closed = False

    def register(self, project, session_id):
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue(maxsize=1)
        identity = uuid.uuid4().hex
        with self._lock:
            if self._closed or len(self._listeners) >= self.maximum:
                raise HubError('attention_stream_unavailable', 503)
            if sum(value['session_id'] == session_id for value in self._listeners.values()) >= 2:
                raise HubError('attention_stream_unavailable', 429)
            self._listeners[identity] = dict(project=project, session_id=session_id, loop=loop, queue=queue, scheduled=False)
        return identity, queue

    @property
    def closed(self):
        with self._lock:
            return self._closed

    def unregister(self, identity):
        with self._drained:
            self._listeners.pop(identity, None)
            self._drained.notify_all()

    @staticmethod
    def _offer(queue):
        if not queue.full():
            queue.put_nowait(None)

    def consumed(self, identity):
        with self._lock:
            value = self._listeners.get(identity)
            if value is not None:
                value['scheduled'] = False

    def signal(self, project):
        with self._lock:
            listeners = [value for value in self._listeners.values()
                         if value['project'] == project and not value['scheduled']]
            for value in listeners:
                value['scheduled'] = True
        for value in listeners:
            try:
                value['loop'].call_soon_threadsafe(self._offer, value['queue'])
            except RuntimeError:
                pass  # Dead stream; durable intent remains available.

    def close(self):
        with self._lock:
            self._closed = True
            listeners = list(self._listeners.values())
        for value in listeners:
            try:
                value['loop'].call_soon_threadsafe(self._offer, value['queue'])
            except RuntimeError:
                pass

    def wait_closed(self):
        """Retain owners until every registered stream has finished cleanup."""
        with self._drained:
            while self._listeners:
                self._drained.wait()
