"""Opt-in, read-only pytest loop ownership diagnostics; not a production hook.

Invoke explicitly with ``-p tests._hub_loop_diagnostics -s``. We do not call
get_event_loop(), create/close/set loops, force GC, or suppress warnings.

Focused Linux invocation (inside the isolated validation image):

    python -X tracemalloc=10 -m pytest -W default -s \
        -p tests._hub_loop_diagnostics \
        tests/test_presence.py::test_registered_subscription_is_not_reported_as_live_presence \
        tests/test_presence.py::test_disabled_publisher_reports_native_push_unavailable \
        tests/test_presence.py::test_successful_open_mcp_get_sse_is_counted_until_disconnect \
        --basetemp /tmp/presence-loop-diag -o cache_dir=/tmp/pytest-cache

Do not enable tracing globally for the full suite: an existing streaming-memory
test starts/stops its own tracing window and would invalidate global measurements.
"""
import asyncio

import pytest


def _state():
    policy = asyncio.get_event_loop_policy()
    local = getattr(policy, "_local", None)
    loop = getattr(local, "_loop", None) if local is not None else None
    if loop is None:
        return "none"
    return f"{type(loop).__name__}:{id(loop)}:closed={loop.is_closed()}"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    before = _state()
    yield
    print(f"LOOP_OWNERSHIP {item.nodeid} call_before={before} call_after={_state()}")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    yield
    print(f"LOOP_OWNERSHIP {item.nodeid} after_teardown={_state()}")
