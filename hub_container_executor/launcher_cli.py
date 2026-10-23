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
    code, phase = 2, 'unavailable_preserve_evidence'
    try:
        previous = _install_handlers(stop)
        if stop.requested:
            code, phase = 130, 'interrupted_before_launch'
        else:
            supervisor = _load_supervisor()(
                enrollment_file=options.enrollment_file,
                signing_key_file=options.signing_key_file,
                journal_file=options.journal_file, create_journal=True)
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
                    elif not stop.requested:
                        raise RuntimeError('receipt readiness unavailable')
                requested = stop.requested or supervisor.wait_for_stop_with_observations(
                    timeout_ms=options.wait_seconds * 1000, stop_requested=lambda: stop.requested)
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
        if supervisor is not None:
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
    try:
        options = parser.parse_args(argv)
        paths = (options.enrollment_file, options.signing_key_file, options.journal_file)
        if (not all(_path(value) for value in paths) or len(set(paths)) != 3
                or not 1 <= options.wait_seconds <= 3600):
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
