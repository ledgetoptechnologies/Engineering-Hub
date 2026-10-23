"""Explicit fixed-code installed manual-session selector; no automatic activation.

Existing normal Hub startup and unified-pilot CLI remain unchanged. An operator
must explicitly select this module and independently provision protected files.
"""
import argparse
import asyncio

from .unified_pilot_startup import serve_configuration


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--config', required=True)
    parser.add_argument('--pilot-config', required=True)
    parser.add_argument('--session-config', required=True)
    args = parser.parse_args(argv)
    try:
        with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
            runner.run(serve_configuration(args.config, args.pilot_config,
                session_enrollment_path=args.session_config))
        return 0
    except (Exception, KeyboardInterrupt):
        parser.exit(2, 'Trusted manual staging startup unavailable\n')


if __name__ == '__main__':
    main()
