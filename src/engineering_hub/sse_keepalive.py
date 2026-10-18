"""Transport-only idle heartbeats around one existing event subscription."""
from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import TypeVar

T = TypeVar("T")


def with_keepalive(source: AsyncIterator[T], *, interval: float = 15.0) -> AsyncIterator[T | None]:
    """Yield original events or ``None`` for an idle transport keepalive.

    Timeouts never cancel/restart the single pending source read. No cursor,
    database or network operation is manufactured by a heartbeat. Callers must
    close this iterator on early exit (for example with ``contextlib.aclosing``).
    Source events should not themselves be None, the transport sentinel.
    """
    if (type(interval) not in (int, float) or not math.isfinite(interval)
            or not 0 < interval <= 300):
        raise ValueError("SSE keepalive interval must be finite and between zero and 300 seconds")
    iterator = aiter(source)

    async def iterate():
        pending = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(anext(iterator))
                done, _ = await asyncio.wait({pending}, timeout=interval)
                if not done:
                    yield None
                    continue
                try:
                    item = pending.result()
                except StopAsyncIteration:
                    return
                pending = None
                yield item
        finally:
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await pending
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()

    return iterate()
