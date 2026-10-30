"""Read-only checks run as the proposed observer service identity; not deployment acceptance."""
import argparse
import json
import os
from pathlib import Path
import stat

from .runtime_inventory import _read_host, _secure_directory, _utc, _validate_host


def check(snapshot_path, token_path, expected_uid, expected_gid):
    result = {'identity': 'unavailable', 'snapshot': 'unavailable', 'credential': 'unavailable',
              'firewall_admission_tls_acceptance': 'not_assessed'}
    if os.name != 'posix' or expected_uid <= 0 or expected_gid <= 0:
        return result
    if os.geteuid() != expected_uid or os.getegid() != expected_gid:
        return result
    result['identity'] = 'observed'
    try:
        source, _, _ = _validate_host(_read_host(snapshot_path), _utc())
        # A stale snapshot is readable but is not current observer evidence.
        result['snapshot'] = 'observed' if source['state'] == 'observed' else 'unavailable'
    except (OSError, ValueError, TypeError, KeyError):
        pass
    try:
        path = Path(token_path)
        directory = _secure_directory(path.parent)
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != expected_uid or stat.S_IMODE(info.st_mode) & 0o077
                        or info.st_size > 4096):
                    raise ValueError('credential_protection')
                # Validate the opened file itself; never print its contents.
                raw = os.read(fd, 4097)
                token = raw.decode('utf-8').strip()
                if not token.isascii() or not 16 <= len(token) <= 4096 or '\n' in token or '\r' in token:
                    raise ValueError('credential_invalid')
            finally:
                os.close(fd)
        finally:
            os.close(directory)
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

