"""Opt-in in-process staging consumer of the existing native Hub feed.

No new watcher, clock polling, model invocation or host capability is created.
Shutdown waits for an active bounded workflow at its safe boundary; only an
idle event wait is cancelled. Unknown command outcomes stay in durable receipts.
"""
import asyncio


class StagingRuntime:
    def __init__(self, dispatcher, feed):
        self.dispatcher, self.feed = dispatcher, feed
        self._task = None
        self._stop = asyncio.Event()
        self.state = 'not_started'
        self.last_result = None

    @property
    def status(self):
        return {'configured': True, 'state': self.state,
                'running': self._task is not None and not self._task.done(),
                'last_result': self.last_result}

    async def start(self):
        if self._task is not None:
            raise RuntimeError('staging_runtime_already_started')
        if not self.feed.available:
            self.state = 'native_source_unavailable'
            return self.status
        self._stop.clear()
        self.state = 'active'
        self._task = asyncio.create_task(self._run())
        return self.status

    async def stop(self):
        self._stop.set()
        if self._task is not None:
            # Do not force-kill an accepted deploy, smoke or rollback command.
            # The typed executor client supplies bounded operation timeouts.
            await self._task
        if self.state not in {'failed', 'native_source_unavailable'}:
            self.state = 'stopped'

    async def _run(self):
        source = self.feed.broker.events()
        try:
            while not self._stop.is_set():
                event = asyncio.create_task(anext(source))
                stopped = asyncio.create_task(self._stop.wait())
                try:
                    done, _ = await asyncio.wait((event, stopped), return_when=asyncio.FIRST_COMPLETED)
                    if stopped in done:
                        break
                    pointer = event.result()
                finally:
                    for pending in (event, stopped):
                        if not pending.done():
                            pending.cancel()  # Only an idle subscription wait.
                    await asyncio.gather(event, stopped, return_exceptions=True)
                if pointer['kind'] == 'source_unavailable':
                    self.state = 'native_source_unavailable'
                    return
                while not self._stop.is_set():
                    if not self.feed.available:
                        self.state = 'native_source_unavailable'
                        return
                    # One history row per bounded step gives shutdown a safe
                    # boundary without interrupting a running workflow.
                    result = await self.dispatcher.drain_batch(limit=1)
                    self.last_result = result
                    if result['paused'] or not result['more']:
                        break
                    await asyncio.sleep(0)  # Fairness, not a timed status query.
        except StopAsyncIteration:
            self.state = 'native_source_unavailable'
        except Exception:
            self.state = 'failed'  # No peer titles, endpoint or secret errors.
        finally:
            await source.aclose()

