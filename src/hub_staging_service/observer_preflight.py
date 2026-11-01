"""Read-only checks run as the proposed observer service identity; not deployment acceptance."""
import argparse
import json
import os
from pathlib import Path

from .runtime_inventory import _read_host, _utc, _validate_host
from .observer_credentials import read_observer_credential, restricted_identity


def check(snapshot_path, token_path, expected_uid, expected_gid):
    result = {'identity': 'unavailable', 'snapshot': 'unavailable', 'credential': 'unavailable',
              'firewall_admission_tls_acceptance': 'not_assessed'}
    if os.name != 'posix' or expected_uid <= 0 or expected_gid <= 0:
        return result
    if not restricted_identity() or os.geteuid() != expected_uid or os.getegid() != expected_gid:
        return result
    result['identity'] = 'observed'
    try:
        source, _, _ = _validate_host(_read_host(snapshot_path), _utc())
        # A stale snapshot is readable but is not current observer evidence.
        result['snapshot'] = 'observed' if source['state'] == 'observed' else 'unavailable'
    except (OSError, ValueError, TypeError, KeyError):
        pass
    try:
        read_observer_credential(token_path, expected_uid)
        result['credential'] = 'observed'
    except (OSError, ValueError, UnicodeError):
        pass
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True, type=Path)
    parser.add_argument('--token-file', required=True, type=Path)
    parser.add_argument('--expected-uid', required=True, type=int)
    parser.add_argument('--expected-gid', required=True, type=int)
    args = parser.parse_args()
    result = check(args.snapshot, args.token_file, args.expected_uid, args.expected_gid)
    print(json.dumps(result))
    return 0 if all(result[key] == 'observed' for key in ('identity', 'snapshot', 'credential')) else 1


if __name__ == '__main__':
    raise SystemExit(main())
