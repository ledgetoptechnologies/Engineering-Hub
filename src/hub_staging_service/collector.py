"""Root-only fixed read-only staging inventory collector; no listener or installer."""
import argparse
import os
from pathlib import Path
import sys
import time

from hub_staging_service.runtime_inventory import collect_host, publish_host


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path,
                        help='Snapshot in an existing root-owned, non-writable-by-worker directory')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if getattr(os, 'geteuid', lambda: -1)() != 0:
        parser.error('root_collector_required')
    try:
        while True:
            publish_host(collect_host(diagnostic=lambda code: print(code, file=sys.stderr)), args.output)
            if args.once:
                return 0
            time.sleep(30)
    except KeyboardInterrupt:
        return 0
    except Exception:
        # Never emit command output, arbitrary paths, credentials or exception text.
        print('staging_runtime_snapshot_unavailable', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
