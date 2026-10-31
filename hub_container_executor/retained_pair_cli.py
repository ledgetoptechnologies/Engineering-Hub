"""Foreground root owner for one explicitly enrolled local TEST pair.

The prebound broker listener is an inherited descriptor. After the executor
receipt, SIGUSR1 announces that an independent operator has placed the fresh
root-protected handoff at the enrolled path. SIGINT/SIGTERM request orderly
drain only after pair startup. An uncertain result remains foreground-held;
this module never adopts, retries, force-kills or releases held resources.
"""
import argparse
import json
import os
import signal
import socket
import stat
import sys
import threading
import time
from pathlib import Path

from .launcher_cli import _host_available
from .launch_supervisor import LaunchSupervisor, _identity, _path, _root_file, _root_parent, _strict
from .retained_oauth_pair import RetainedOAuthPair
from .github_broker_service import read_enrollment


class OperatorError(RuntimeError):
    pass


def _require(value):
    if not value: raise OperatorError('retained pair unavailable; preserve evidence')


def _emit(state, **fields):
    print(json.dumps({'state': state, **fields}, sort_keys=True, separators=(',', ':')), flush=True)


class _Signals:
    def __init__(self): self.ready = False; self.stop = False
    def on_ready(self, *_): self.ready = True
    def on_stop(self, *_): self.stop = True


def _install_signals(state):
    previous = []
    try:
        for number, handler in ((signal.SIGUSR1, state.on_ready), (signal.SIGTERM, state.on_stop),
                                (signal.SIGINT, state.on_stop), (signal.SIGHUP, state.on_stop)):
            old = signal.getsignal(number); signal.signal(number, handler); previous.append((number, old))
        return previous
    except BaseException:
        for number, old in reversed(previous): signal.signal(number, old)
        raise


def _park_held():
    # No unattended re-admission or descriptor release: an operator must apply
    # a separately reviewed manual recovery to the original held processes.
    while True: signal.pause()


def _startup(raw):
    data = _strict(raw, 16384)
    names = {'schema', 'executor_enrollment', 'executor_key', 'executor_journal', 'held_ranges',
             'handoff_file', 'broker_listener_fd', 'report_wait_seconds', 'run_wait_seconds',
             'executor_stop_timeout_ms', 'broker_stop_seconds'}
    _require(type(data) is dict and set(data) == names and data['schema'] == 'engineering-hub.retained-pair-operator.v1')
    paths = [str(_path(data[k])) for k in ('executor_enrollment','executor_key','executor_journal','held_ranges','handoff_file')]
    _require(len(set(paths)) == len(paths))
    _require(type(data['broker_listener_fd']) is int and 3 <= data['broker_listener_fd'] < 2**20)
    _require(all(type(data[k]) is int and 1 <= data[k] <= 3600 for k in ('report_wait_seconds','run_wait_seconds'))
             and type(data['executor_stop_timeout_ms']) is int and 1000 <= data['executor_stop_timeout_ms'] <= 86_415_000
             and type(data['broker_stop_seconds']) is int and 1 <= data['broker_stop_seconds'] <= 120)
    return data


def _handoff(raw):
    data = _strict(raw, 16384)
    names = {'schema','pair_policy','config_file','session_file','admission_file','coordinator_enrollment',
             'coordinator_key','coordinator_journal','held_ranges','broker_enrollment'}
    _require(type(data) is dict and set(data) == names and data['schema'] == 'engineering-hub.retained-pair-handoff.v1')
    paths = [str(_path(data[k])) for k in names - {'schema'}]
    _require(len(set(paths)) == len(paths))
    return data


def _broker_fd(path):
    path = _path(path); parent = _root_parent(path, private=True); fd = None
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
        def metadata(info):
            return (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid,info.st_nlink,
                    info.st_size,info.st_mtime_ns,info.st_ctime_ns)
        before = os.fstat(fd); named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        _require(stat.S_ISREG(before.st_mode) and metadata(before) == metadata(named)
                 and _identity(os.fstat(parent)) == _identity(os.lstat(path.parent)))
        read_enrollment(fd)
        after = os.fstat(fd); named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        _require(metadata(before) == metadata(after) == metadata(named)
                 and _identity(os.fstat(parent)) == _identity(os.lstat(path.parent)))
        result = fd; fd = None; return result
    finally:
        try:
            if fd is not None: os.close(fd)
        finally: os.close(parent)


def _execute(options):
    from .installed_coordinator import MAX_CONFIG_BYTES
    from .coordinator_admission import MAX_BYTES as MAX_ADMISSION_BYTES
    from engineering_hub.trusted_queued_session import MAX_ENROLLMENT_BYTES
    state = _Signals(); previous = []; executor = owner = listener = None
    broker_fd = parent = None; attempted = False; stopped = False; cleanup_failed = False
    try:
        raw, _ = _root_file(_path(options.operator_file), 16384); setup = _startup(raw)
        listener = socket.socket(fileno=setup['broker_listener_fd'])
        _require(listener.family == socket.AF_UNIX and listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) == socket.SOCK_STREAM
                 and listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1)
        handoff_path = _path(setup['handoff_file']); parent = _root_parent(handoff_path, private=True)
        parent_identity = _identity(os.fstat(parent))
        try: os.stat(handoff_path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError: pass
        else: raise OperatorError('handoff must be fresh')
        previous = _install_signals(state)
        _require(not state.stop)
        executor = LaunchSupervisor(enrollment_file=setup['executor_enrollment'], signing_key_file=setup['executor_key'],
            journal_file=setup['executor_journal'], create_journal=True, held_ranges_file=setup['held_ranges'])
        _require(executor.profile.role is None and executor.profile.executor_endpoint == 'protected-uds-v1'
                 and executor.profile.gate_ledger_channel == 'pair-outcome-listener-v1')
        _require(not state.stop)
        attempted = True
        executor.launch()
        _require(executor.wait_for_receipt_request(timeout_ms=60000, stop_requested=lambda: state.stop) and not state.stop)
        executor.serve_one_receipt()
        _emit('executor_ready', owner_pid=os.getpid(), launch_id=executor._launch_id,
              container_id=executor._container_id, boot_id=executor._consumer.boot,
              pid=executor._consumer.pid, start_ticks=executor._consumer.start,
              approval_expires_at_ms=executor._approval_expires_at_ms)
        report_deadline = time.monotonic() + setup['report_wait_seconds']
        def report_current():
            _require(time.monotonic() < report_deadline and not state.stop)
        _require(executor.wait_for_stop_with_observations(timeout_ms=setup['report_wait_seconds']*1000,
                    stop_requested=lambda: state.ready or state.stop) and state.ready and not state.stop)
        report_current()
        _require(_identity(os.fstat(parent)) == parent_identity == _identity(os.lstat(handoff_path.parent)))
        handoff_raw, handoff_pin = _root_file(handoff_path, 16384); handoff = _handoff(handoff_raw)
        report_current()
        config, _ = _root_file(_path(handoff['config_file']), MAX_CONFIG_BYTES)
        report_current()
        session, _ = _root_file(_path(handoff['session_file']), MAX_ENROLLMENT_BYTES)
        report_current()
        admission, _ = _root_file(_path(handoff['admission_file']), MAX_ADMISSION_BYTES)
        report_current()
        broker_fd = _broker_fd(handoff['broker_enrollment'])
        report_current()
        _require(_root_file(handoff_path, 16384)[1] == handoff_pin
                 and _identity(os.fstat(parent)) == parent_identity == _identity(os.lstat(handoff_path.parent))
                 and not state.stop)
        owner = RetainedOAuthPair(executor=executor, pair_policy=handoff['pair_policy'], config_raw=config,
            session_raw=session, admission_raw=admission, broker_enrollment_fd=broker_fd, broker_listener=listener)
        report_current()
        owner.create_coordinator(enrollment_file=handoff['coordinator_enrollment'], signing_key_file=handoff['coordinator_key'],
            journal_file=handoff['coordinator_journal'], held_ranges_file=handoff['held_ranges'])
        # This final check is the one startup admission boundary. Once start()
        # begins, a signal requests drain after that admitted work finishes.
        report_current()
        owner.start()
        _emit('pair_running', owner_pid=os.getpid(), pair_id=owner._policy['pair_id'])
        requested = owner.wait(timeout_ms=setup['run_wait_seconds']*1000, stop_requested=lambda: state.stop)
        _require(requested and state.stop)
        coordinator_timeout = owner._coordinator._coordinator_stop_grace*1000 + owner._coordinator._coordinator_admission.timeout_ms + 5000
        owner.stop(coordinator_timeout_ms=coordinator_timeout, broker_timeout_seconds=setup['broker_stop_seconds'],
                   executor_timeout_ms=setup['executor_stop_timeout_ms'])
        owner.close(); stopped = True
    except BaseException:
        if attempted:
            if owner is not None:
                try: owner._hold()
                except BaseException: pass
            elif executor is not None:
                try:
                    with executor._mutex: executor._hold()
                except BaseException: pass
            try:
                _emit('held', owner_pid=os.getpid(), launch_id=getattr(executor, '_launch_id', None),
                      container_id=getattr(executor, '_container_id', None))
            except BaseException:
                pass  # A broken status pipe never releases the retained authority.
            while True: _park_held()
    finally:
        # Held code never reaches this cleanup; owners and descriptors stay live.
        if not attempted or stopped:
            def close(action):
                nonlocal cleanup_failed
                try: action()
                except BaseException: cleanup_failed = True
            if listener is not None: close(listener.close)
            if broker_fd is not None: close(lambda: os.close(broker_fd))
            if parent is not None: close(lambda: os.close(parent))
            if executor is not None and owner is None: close(executor.close)
            for number, old in reversed(previous): close(lambda number=number, old=old: signal.signal(number, old))
    if cleanup_failed:
        _emit('cleanup_uncertain'); return 2
    if stopped:
        _emit('stopped'); return 0
    _emit('unavailable'); return 2


def main(argv=None):
    class Parser(argparse.ArgumentParser):
        def error(self, message): raise OperatorError() from None
    try:
        parser = Parser(allow_abbrev=False)
        parser.add_argument('--operator-file', required=True)
        parser.add_argument('--execute', action='store_true')
        options = parser.parse_args(argv); _path(options.operator_file)
        if not options.execute:
            _emit('not_executed'); return 0
        _require(_host_available() and threading.current_thread() is threading.main_thread())
    except Exception:
        _emit('invalid_operator_request'); return 2
    return _execute(options)


if __name__ == '__main__':
    raise SystemExit(main())
