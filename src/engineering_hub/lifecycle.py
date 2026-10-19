"""Shared cancellation-safe lifecycle cleanup without private state imports."""
import asyncio


async def finish_owned(awaitable):
    """Finish accepted cleanup despite repeated caller cancellation, then propagate it.

    There is deliberately no timeout or force-cancel: releasing a lifecycle lock
    while accepted work still runs would make a second controller unsafe.
    """
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
        except BaseException:
            # Read the final outcome below, including when cancellation preceded
            # a child failure. Never include exception text in lifecycle status.
            break
    try:
        result = task.result()
    except BaseException:
        if cancelled:
            raise asyncio.CancelledError from None
        raise
    if cancelled:
        raise asyncio.CancelledError
    return result
