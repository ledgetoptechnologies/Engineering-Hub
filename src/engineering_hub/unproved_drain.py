"""Retain an original resource graph when its staging drain is unproved.

The actual owner lock stays held; this is not a global selector reservation or
a restart lock. Process death still requires durable-state reconciliation.
"""
import asyncio
import logging


_held = []


async def retain_unproved_drain(*resources):
    """Hold resources and their event loop until explicit process recovery.

    No timeout, cancellation, garbage collection, or caller assertion proves
    that accepted effects ended. There is intentionally no automatic release or
    retry. Admission must already be closed by the original lifecycle.
    """
    loop = asyncio.get_running_loop()
    # Publish strong references before the first await, including the event loop.
    _held.append((loop, resources))
    logging.getLogger(__name__).error('owned_staging_drain_unproved_resources_retained')
    waiter = loop.create_future()
    while True:
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            continue
