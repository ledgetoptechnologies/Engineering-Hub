"""Explicit inert container entrypoint; never constructs the Hub runtime.

Standby proves only its own mode, not exclusion of other database writers.
"""
import argparse
import json
import signal
import time


def standby():
    """Wait for an operator stop without opening databases or listeners."""
    stopped = False
    previous = {}

    def request_stop(signum, frame):
        # No lock acquisition in a Python signal handler: another signal can
        # interrupt it. In particular threading.Event.set() is not safe here.
        nonlocal stopped
        stopped = True

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, request_stop)
        print(json.dumps({
            'status': 'maintenance_standby_no_runtime',
            'all_writers_stopped': 'not_established',
            'database_opened': False,
            'network_listener_started': False,
            'owner_constructed': False,
            'healthcheck_must_be_disabled_by_operator': True,
        }, sort_keys=True), flush=True)
        while not stopped:
            time.sleep(0.1)
        print(json.dumps({'status': 'maintenance_standby_stopped',
                          'enrollment_or_runtime_started': False}, sort_keys=True), flush=True)
        return 0
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv=None):
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            self.exit(2, 'Explicit --standby is required; no runtime started\n')

    parser = Parser(description='Inert container maintenance standby; no enrollment.',
                    allow_abbrev=False)
    parser.add_argument('--standby', action='store_true', required=True)
    parser.parse_args(argv)
    return standby()


if __name__ == '__main__':
    raise SystemExit(main())
