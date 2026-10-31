"""Explicit one-launch Linux host-root operator, not a service or installer.

Run with ``python -I -m hub_container_executor.launcher_cli`` from an approved
installed environment. Existing protected enrollment and signing key are inputs;
the fresh journal path must be selected and its existing parent independently
provisioned as required by LaunchSupervisor. No key, enrollment, parent directory
or trust is created here. Missing --execute performs no supervisor construction,
key read or filesystem access.

SIGUSR1, SIGTERM and SIGINT request ONE graceful stop. FIRST drain the coordinator
and all accepted jobs externally: the native stop deadline is at most 120 seconds,
not the application's much longer busy-work grace period. This command does not
prove drain, provide busy-work recovery, or install restart/boot behavior.

Cold-start listener readiness has a separate maximum 60-second wait clipped by
the enrolled receipt expiry. It observes the owned consumer, not an Engine poll;
the authenticated exchange retains its original enrolled deadline. Stop signals
during readiness cancel that wait before one explicit owned graceful stop.

--wait-seconds bounds ONLY waiting after the one receipt exchange, during which
the SAME retained supervisor serves bounded current observations. It does not
extend receipt authority and is not a total-process deadline. Native enrollment
operation deadlines remain authoritative. Expiry or any uncertain native outcome
closes descriptors only, returns nonzero and preserves evidence; it never grants
an automatic SIGTERM, retry, adoption, removal or forced kill. A stop during the
synchronous receipt exchange is acted on after it returns successfully, never
from a signal handler or by a second thread inside the supervisor's mutex.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import PurePosixPath
import signal
import socket
import sys
import threading
import time


class _UsageError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default includes supplied values. Do not reflect arguments.
        raise _UsageError() from None


@dataclass
class _Stop:
    requested: bool = False

    def request(self, signum, frame):
        # Intentionally no locks, I/O, native methods, exceptions or logging.
        self.requested = True


def _path(value):
    return (isinstance(value, str) and value.startswith('/') and value != '/'
            and not value.startswith('//') and str(PurePosixPath(value)) == value
            and all(ord(char) >= 32 and ord(char) != 127 for char in value)
            and '..' not in PurePosixPath(value).parts)


def _host_available():
    return (sys.platform == 'linux' and callable(getattr(os, 'getuid', None))
            and callable(getattr(os, 'geteuid', None))
            and os.getuid() == os.geteuid() == 0
            and hasattr(os, 'pidfd_open') and hasattr(socket, 'SO_PEERCRED')
            and hasattr(signal, 'SIGUSR1')
            and threading.current_thread() is threading.main_thread())


def _load_supervisor():
    # Do not read enrollment/key or import Linux-only native code for dry use.
    from .launch_supervisor import LaunchSupervisor
    return LaunchSupervisor


def _load_gate_operation(path):
    from .gate_host_enrollment import load_gate_host_enrollment
    return load_gate_host_enrollment(path)


def _preflight_gate_operation(operation, supervisor):
    from .gate_host_enrollment import preflight_gate_host_enrollment
    preflight_gate_host_enrollment(operation, supervisor)


def _install_handlers(stop):
    previous = []
    try:
        for number in (signal.SIGUSR1, signal.SIGTERM, signal.SIGINT):
            old = signal.getsignal(number)
            signal.signal(number, stop.request)
            previous.append((number, old))
    except BaseException:
        _restore_handlers(previous)
        raise
    return previous


def _restore_handlers(previous):
    failed = False
    for number, old in reversed(previous):
        try:
            signal.signal(number, old)
        except BaseException:
            failed = True
    if failed:
        raise RuntimeError('handler restoration unavailable') from None


def _wait_for_stop(stop, seconds):
    deadline = time.monotonic() + seconds
    while not stop.requested:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.1, remaining))
    return True


def _execute(options):
    stop = _Stop()
    previous = []
    supervisor = None
    operation = None
    gate_thread = None
    gate_cancel, gate_failed = threading.Event(), threading.Event()
    retain_supervisor = False
    code, phase = 2, 'unavailable_preserve_evidence'
    try:
        previous = _install_handlers(stop)
        if stop.requested:
            code, phase = 130, 'interrupted_before_launch'
        else:
            gate_file = getattr(options, 'gate_operation_file', None)
            if gate_file is not None:
                operation = _load_gate_operation(gate_file)
            # Only v3 enrollments take held-range evidence; legacy calls stay exactly as before.
            held = {} if options.held_ranges_file is None else {'held_ranges_file': options.held_ranges_file}
            if operation is not None: held['gate_start_mode'] = True
            supervisor = _load_supervisor()(
                enrollment_file=options.enrollment_file,
                signing_key_file=options.signing_key_file,
                journal_file=options.journal_file, create_journal=True, **held)
            if operation is not None:
                _preflight_gate_operation(operation, supervisor)
            if stop.requested:
                code, phase = 130, 'interrupted_before_launch'
            else:
                supervisor.launch()
                # Cold-start imports must not consume the frame-exchange budget.
                # The callback reads a signal flag only, outside native locks.
                if not stop.requested:
                    ready = supervisor.wait_for_receipt_request(timeout_ms=60000,
                        stop_requested=lambda: stop.requested)
                    if ready and not stop.requested:
                        supervisor.serve_one_receipt()
                        print('receipt_served', flush=True)
                        if operation is not None:
                            def run_gate():
                                try:
                                    operation.check()
                                    supervisor.run_gate_operation(manifest=operation.manifest, image=operation.image,
                                        enrollment_file=operation.application_enrollment_file,
                                        controller_key_file=operation.controller_key_file,
                                        timeout_seconds=options.gate_timeout_seconds,
                                        stop_requested=lambda: stop.requested or gate_cancel.is_set(),
                                        authority_check=operation.check)
                                    operation.check()
                                except BaseException:
                                    gate_failed.set()
                                    supervisor._held, supervisor._issuance_open, supervisor._hold_persisted = True, False, False
                                    if supervisor._mutex.acquire(timeout=1):
                                        try: supervisor._hold()
                                        finally: supervisor._mutex.release()
                            gate_thread = threading.Thread(target=run_gate, name='hub-owned-gate', daemon=False)
                            gate_thread.start()
                    elif not stop.requested:
                        raise RuntimeError('receipt readiness unavailable')
                requested = stop.requested or supervisor.wait_for_stop_with_observations(
                    timeout_ms=options.wait_seconds * 1000,
                    stop_requested=lambda: stop.requested or gate_failed.is_set())
                if gate_thread is not None:
                    gate_cancel.set(); gate_thread.join(timeout=6)
                    if gate_thread.is_alive():
                        retain_supervisor = True
                        raise RuntimeError('owned gate remains active')
                    if gate_failed.is_set(): raise RuntimeError('gate unavailable')
                if requested:
                    # Explicit signal only. Caller must already have drained.
                    supervisor.graceful_stop()
                    code, phase = 0, 'stopped'
                else:
                    phase = 'wait_expired_preserve_evidence'
    except BaseException:
        # Including interrupted/partial native effects: no automatic stop/retry.
        code, phase = 2, 'unavailable_preserve_evidence'
    finally:
        if gate_thread is not None:
            if gate_thread.is_alive():
                gate_cancel.set(); gate_thread.join(timeout=6)
            retain_supervisor = gate_thread.is_alive()
        if supervisor is not None and not retain_supervisor:
            try:
                supervisor.close()  # FDs only; never certifies a stopped process.
            except BaseException:
                if code == 0:
                    code, phase = 2, 'close_failed_preserve_evidence'
        try:
            _restore_handlers(previous)
        except BaseException:
            if code == 0:
                code, phase = 2, 'handler_restore_failed'
    return code, phase


def main(argv=None):
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--enrollment-file', required=True)
    parser.add_argument('--signing-key-file', required=True)
    parser.add_argument('--journal-file', required=True)
    parser.add_argument('--wait-seconds', required=True, type=int,
                        help='post-receipt stop-request waiting bound, 1..3600; expiry does not stop')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--create-journal', action='store_true',
                        help='require fresh native journal; existing journal/lock is rejected')
    parser.add_argument('--held-ranges-file', default=None,
                        help='v3 only: root-protected held TEST ID ranges, an operator attestation')
    parser.add_argument('--gate-operation-file', default=None,
                        help='explicit root-protected v5 gate operation approval')
    parser.add_argument('--gate-timeout-seconds', type=int, default=None,
                        help='required with gate operation; bounded to 1..120 seconds')
    try:
        options = parser.parse_args(argv)
        paths = (options.enrollment_file, options.signing_key_file, options.journal_file)
        if options.held_ranges_file is not None:
            paths += (options.held_ranges_file,)
        if options.gate_operation_file is not None:
            paths += (options.gate_operation_file,)
        if (not all(_path(value) for value in paths) or len(set(paths)) != len(paths)
                or not 1 <= options.wait_seconds <= 3600
                or ((options.gate_operation_file is None) != (options.gate_timeout_seconds is None))
                or (options.gate_timeout_seconds is not None and not 1 <= options.gate_timeout_seconds <= 120)):
            raise _UsageError()
        if not options.execute:
            print('not_executed', flush=True)
            return 0
        if not options.create_journal or not _host_available():
            raise _UsageError()
    except _UsageError:
        print('invalid_operator_request', file=sys.stderr, flush=True)
        return 2
    code, phase = _execute(options)
    print(phase, flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
