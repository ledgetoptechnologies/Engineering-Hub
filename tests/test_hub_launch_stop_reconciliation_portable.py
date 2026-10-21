"""Actual native method control flow with synthetic DTOs, not kernel acceptance.

Loading only these AST methods lets Windows exercise the unchanged full-profile
checks and shutdown accounting logic without importing Linux-only fcntl or
constructing an authority, accessing a key, or connecting to any Engine.
"""
import ast
from copy import deepcopy
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest


def authority_fixture(states, *, error=None, response_delay=0):
    source = Path(__file__).parents[1] / 'hub_container_executor/launch_supervisor.py'
    parsed = ast.parse(source.read_text(encoding='utf-8'))
    native = next(item for item in parsed.body if isinstance(item, ast.ClassDef)
                  and item.name == 'LaunchSupervisor')
    names = {'_create_body', '_inspect', '_wait_stopped', 'graceful_stop'}
    methods = [node for node in native.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in methods} == names
    clock = SimpleNamespace(now=100., sleeps=[])
    def sleep(duration):
        clock.sleeps.append(duration)
        clock.now += duration
    def require(condition):
        if not condition:
            raise RuntimeError('synthetic closed failure')
    def remaining(deadline):
        require(deadline > clock.now)
        return deadline - clock.now
    namespace = {'os': os, 'time': SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep),
                 '_require': require, '_remaining': remaining, 'LaunchSupervisorError': RuntimeError,
                 'select': SimpleNamespace(select=lambda *args: ([17], [], []))}
    body = ast.Module(body=[ast.ClassDef(name='Native', bases=[], keywords=[], body=methods,
                                       decorator_list=[])], type_ignores=[])
    exec(compile(ast.fix_missing_locations(body), str(source), 'exec'), namespace)
    authority = namespace['Native']()
    authority._pid, authority._mutex = os.getpid(), threading.Lock()
    authority._launch_ms = 1000
    authority._consumer = SimpleNamespace(pid=123, fd=17)
    authority._container_id, authority._launch_id = 'a' * 64, 'synthetic-launch'
    authority._issuance_open, authority._held = True, False
    authority.profile = SimpleNamespace(user='10001:10001', entrypoint=('python',),
        command=('-m', 'approved'), environment=('PATH=/bin',), memory_bytes=1024,
        cpu_millicores=100, pids_limit=32, supplementary_groups=(), tmpfs_bytes=1024,
        mounts=())
    authority.image = SimpleNamespace(runnable_reference='sha256:' + '1' * 64,
        image_config_id='sha256:' + '2' * 64, platform_manifest_digest='sha256:' + '3' * 64)
    authority._guard = lambda **kwargs: None
    authority._validate_live = lambda *args, **kwargs: None  # Not a kernel fixture.
    authority._record = lambda: {}
    journal, effects, reads, cleanup = [], [], [], []
    authority._journal = SimpleNamespace(state=lambda *args: journal.append(args[1]))
    def hold():
        authority._held = True
        authority._issuance_open = False
        journal.append('held')
    authority._hold = hold
    authority._remove_owned_socket = lambda: cleanup.append('owned socket')
    def request(method, route, deadline, **kwargs):
        if method == 'POST':
            effects.append((route, deadline))
            assert route == '/containers/' + authority._container_id + '/kill?signal=SIGTERM'
            return 204, None
        assert method == 'GET' and route == '/containers/' + authority._container_id + '/json'
        reads.append(deadline)
        if error is not None:
            raise error
        clock.now += response_delay
        state, mutation = states[min(len(reads) - 1, len(states) - 1)]
        original = deepcopy(authority._create_body())
        host = original.pop('HostConfig')
        info = dict(Id=authority._container_id, Image=authority.image.image_config_id,
            Config=original, HostConfig=host, Mounts=[], State=deepcopy(state),
            Path=authority.profile.entrypoint[0],
            Args=list((*authority.profile.entrypoint[1:], *authority.profile.command)))
        if mutation:
            mutation(info)
        return 200, info
    authority._engine = SimpleNamespace(json=request)
    return authority, clock, journal, effects, reads, cleanup


RUNNING = dict(Running=True, Status='running', Pid=123)
EXITED = dict(Running=False, Status='exited', Pid=0)


def test_delayed_engine_accounting_reads_only_under_original_stop_deadline():
    a, clock, journal, effects, reads, cleanup = authority_fixture(
        [(RUNNING, None), (RUNNING, None), (EXITED, None)])
    a.graceful_stop()
    assert journal == ['term_reserved', 'stopped']
    assert len(effects) == 1 and reads == [effects[0][1]] * 3
    assert clock.sleeps == [0.05, 0.05] and cleanup == ['owned socket']
    assert not a._issuance_open and not a._held


def test_accounting_timeout_holds_without_second_signal_or_cleanup():
    a, clock, journal, effects, reads, cleanup = authority_fixture([(RUNNING, None)])
    with pytest.raises(RuntimeError):
        a.graceful_stop()
    assert journal == ['term_reserved', 'held'] and a._held
    assert len(effects) == 1 and 1 < len(reads) <= 21
    assert not cleanup and not a._issuance_open
    assert clock.now <= effects[0][1]


@pytest.mark.parametrize('state,mutation', [
    (RUNNING, lambda info: info.update(Id='b' * 64)),
    (RUNNING, lambda info: info['Config'].update(Env=['SECRET=unapproved'])),
    (RUNNING, lambda info: info['HostConfig'].update(Privileged=True)),
    (RUNNING, lambda info: info.update(Mounts=[{'Destination': '/foreign'}])),
    ({**RUNNING, 'Pid': 124}, None),
    ({**RUNNING, 'Pid': True}, None),
    ({**RUNNING, 'Running': 1}, None),
    ({**RUNNING, 'Restarting': True}, None),
    ({**RUNNING, 'Paused': True}, None),
    ({**RUNNING, 'Dead': True}, None),
    ({**RUNNING, 'Status': 'exited'}, None),
    ({**EXITED, 'Status': 'created'}, None),
    ({**EXITED, 'Pid': 123}, None),
    ({**EXITED, 'Pid': False}, None),
])
def test_changed_or_malformed_state_is_not_retried(state, mutation):
    a, clock, journal, effects, reads, cleanup = authority_fixture(
        [(state, mutation), (EXITED, None)])
    with pytest.raises(RuntimeError):
        a.graceful_stop()
    assert journal == ['term_reserved', 'held'] and a._held
    assert len(effects) == len(reads) == 1
    assert not clock.sleeps and not cleanup


def test_get_failure_is_not_retried():
    a, clock, journal, effects, reads, cleanup = authority_fixture(
        [(EXITED, None)], error=TimeoutError('synthetic read failure'))
    with pytest.raises(RuntimeError):
        a.graceful_stop()
    assert len(effects) == len(reads) == 1
    assert journal[-1] == 'held' and not clock.sleeps and not cleanup


def test_late_terminal_response_does_not_extend_deadline():
    a, _, journal, effects, reads, cleanup = authority_fixture(
        [(EXITED, None)], response_delay=2)
    with pytest.raises(RuntimeError):
        a.graceful_stop()
    assert len(effects) == len(reads) == 1
    assert journal[-1] == 'held' and not cleanup


def test_initial_created_inspection_remains_valid_for_launch():
    a, _, _, effects, reads, _ = authority_fixture([
        (dict(Running=False, Status='created', Pid=0), None)])
    assert a._inspect(101., running=False)['Status'] == 'created'
    assert not effects and len(reads) == 1
