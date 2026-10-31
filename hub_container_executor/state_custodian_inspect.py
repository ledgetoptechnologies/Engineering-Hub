"""Explicit root-enrolled, read-only custodian hold inspection command."""
import argparse
import json
import secrets
import sys
import time

from .launch_supervisor import _canonical, _path, _root_file
from .state_custodian_admission import CustodianAdmissionJournal
from .state_custodian_observation import IDENTITY, verify_held_observation


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError()


def main(argv=None):
    journal = None
    try:
        parser = _Parser(description='Inspect one enrolled custodian hold', allow_abbrev=False)
        parser.add_argument('--enrollment', action='append', required=True)
        args = parser.parse_args(argv)
        if len(args.enrollment) != 1: raise ValueError()
        enrollment_path = _path(args.enrollment[0])
        raw, pin = _root_file(enrollment_path, 16384)
        enrollment = json.loads(raw)
        keys = {'schema', 'journal_enrollment', 'journal_path', 'owner_policy', 'copy_policy',
                'provider_key_path', 'provider_public_key', 'expected'}
        if (type(enrollment) is not dict or set(enrollment) != keys or _canonical(enrollment) != raw
                or type(enrollment['schema']) is not int or enrollment['schema'] != 1
                or type(enrollment['expected']) is not dict or set(enrollment['expected']) != IDENTITY
                or type(enrollment['provider_public_key']) is not str
                or len(enrollment['provider_public_key']) != 64
                or enrollment['provider_public_key'] != enrollment['provider_public_key'].lower()):
            raise ValueError()
        public = bytes.fromhex(enrollment['provider_public_key'])
        paths = {name: _path(enrollment[name]) for name in ('journal_enrollment', 'journal_path',
                 'owner_policy', 'copy_policy', 'provider_key_path')}
        start_wall = time.time_ns() // 1_000_000; start_mono = time.monotonic()
        nonce = secrets.token_hex(32)
        journal = CustodianAdmissionJournal(enrollment_path=paths['journal_enrollment'],
                                            journal_path=paths['journal_path'], create=False, read_only=True)
        report = journal.observe_held(target_id=enrollment['expected']['target_id'], nonce=nonce,
            owner_policy_path=paths['owner_policy'], copy_policy_path=paths['copy_policy'],
            provider_key_path=paths['provider_key_path'], timeout_seconds=5)
        verified = verify_held_observation(_canonical(report), provider_public_key=public, nonce=nonce,
            expected=enrollment['expected'], start_wall_ms=start_wall, start_monotonic=start_mono)
        if _root_file(enrollment_path, 16384)[1] != pin: raise ValueError()
        journal.close(); journal = None
        verified = verify_held_observation(_canonical(report), provider_public_key=public, nonce=nonce,
            expected=enrollment['expected'], start_wall_ms=start_wall, start_monotonic=start_mono)
        print(json.dumps({'status': 'held', 'quiescence': 'unknown', 'operation_id': verified['operation_id'],
                          'fencing_generation': verified['fencing_generation'],
                          'marker_present': verified['marker_present']}, sort_keys=True, separators=(',', ':')))
        return 0
    except Exception:
        print('Custodian hold inspection unavailable', file=sys.stderr)
        return 2
    finally:
        if journal is not None:
            try: journal.close()
            except Exception: pass


if __name__ == '__main__':
    raise SystemExit(main())
