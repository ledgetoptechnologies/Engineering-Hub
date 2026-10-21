"""Real native readiness control flow, synthetic FDs/clock, no host authority.

AST-loading the actual methods avoids Linux-only package imports on Windows.
No key, Engine, journal, native process or signing primitive is constructed.
"""
import ast
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest


def fixture(plan=(), *, expires=200.):
    source = Path(__file__).parents[1] / 'hub_container_executor/launch_supervisor.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    native = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                  and node.name == 'LaunchSupervisor')
    names = {'wait_for_receipt_request', 'serve_one_receipt'}
    methods = [node for node in native.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in methods} == names
    clock = SimpleNamespace(now=100., selects=[], timeouts=[], consumer_checks=0)

    def require(value):
        if not value:
            raise RuntimeError('synthetic denial')

    def remaining(deadline):
        require(deadline > clock.now)
        return deadline - clock.now

    class Listener:
        def fileno(self):
            return 12

        def settimeout(self, seconds):
            clock.timeouts.append(seconds)

        def accept(self):
            raise TimeoutError('synthetic exchange accept')

    namespace = {'os': os, 'time': SimpleNamespace(monotonic=lambda: clock.now),
                 'socket': SimpleNamespace(socket=Listener), '_require': require,
                 '_remaining': remaining, 'LaunchSupervisorError': RuntimeError}
    body = ast.Module(body=[ast.ClassDef(name='Native', bases=[], keywords=[],
        body=methods, decorator_list=[])], type_ignores=[])
    exec(compile(ast.fix_missing_locations(body), str(source), 'exec'), namespace)
    authority = namespace['Native']()
    authority._pid, authority._mutex = os.getpid(), threading.Lock()
    authority._closed = authority._held = False
    authority._issuance_open = True
    authority._expires, authority._request_ms = expires, 300
    authority._listener = Listener()

    def check():
        clock.consumer_checks += 1

    authority._consumer = SimpleNamespace(fd=17, check=check)

    def hold():
        authority._held = True
        authority._issuance_open = False

    authority._hold = hold

    def forbidden_guard(**kwargs):
        raise AssertionError('readiness must not read key/enrollment or call guard')

    authority._guard = forbidden_guard
    actions = iter(plan)

    def select(read, write, error, timeout):
        assert read == [authority._listener, 17] and write == error == []
        clock.selects.append(timeout)
        clock.now += timeout
        action = next(actions, None)
        if isinstance(action, BaseException):
            raise action
        return ([authority._listener] if action == 'ready' else [17] if action == 'exit' else []), [], []

    namespace['select'] = SimpleNamespace(select=select)
    return authority, clock


def test_delayed_startup_then_ready_no_issuance_or_native_guard():
    authority, clock = fixture([None, None, None, 'ready'])
    assert authority.wait_for_receipt_request(timeout_ms=60000) is True
    assert len(clock.selects) == clock.consumer_checks == 4
    assert all(0 < delay <= .1 for delay in clock.selects)
    assert not authority._held and authority._issuance_open


@pytest.mark.parametrize('action', ['exit', OSError('synthetic select failure')])
def test_consumer_exit_or_select_error_holds_once_without_exchange(action):
    authority, clock = fixture([action])
    with pytest.raises(RuntimeError):
        authority.wait_for_receipt_request(timeout_ms=60000)
    assert len(clock.selects) == 1 and authority._held and not authority._issuance_open


@pytest.mark.parametrize('timeout,expires,end', [(250, 200., 100.25), (60000, 100.25, 100.25)])
def test_absolute_startup_timeout_or_receipt_expiry_does_not_reset(timeout, expires, end):
    authority, clock = fixture(expires=expires)
    with pytest.raises(RuntimeError):
        authority.wait_for_receipt_request(timeout_ms=timeout)
    assert clock.now == pytest.approx(end)
    assert authority._held and not authority._issuance_open


def test_stop_flag_evaluated_outside_mutex_cancels_without_holding_or_issuing():
    authority, clock = fixture()

    def requested():
        assert authority._mutex.acquire(blocking=False)
        authority._mutex.release()
        return len(clock.selects) >= 2

    assert authority.wait_for_receipt_request(timeout_ms=60000, stop_requested=requested) is False
    assert len(clock.selects) == 2 and not authority._held and authority._issuance_open


def test_readiness_does_not_change_original_exchange_accept_budget():
    authority, clock = fixture(['ready'])
    assert authority.wait_for_receipt_request(timeout_ms=60000)
    authority._guard = lambda: None  # Only exchange boundary is synthetic here.
    with pytest.raises(RuntimeError):
        authority.serve_one_receipt()
    assert clock.timeouts == [pytest.approx(.3)]
    assert authority._request_ms == 300 and authority._held


@pytest.mark.parametrize('timeout', [True, 0, 60001])
def test_invalid_wait_bound_never_waits(timeout):
    authority, clock = fixture(['ready'])
    with pytest.raises(RuntimeError):
        authority.wait_for_receipt_request(timeout_ms=timeout)
    assert not clock.selects
