#!/usr/bin/env python3
"""Read-only candidate inventory. This never enrolls or starts a service.

The operator must independently select a fresh local path and resource budget.
This does not inspect an Engine, credentials, protected data or running jobs.
"""
import argparse
import grp
import json
import os
from pathlib import Path
import pwd
import stat
import sys


def parse_id_map(text):
    rows = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 3 or any(not field.isascii() or not field.isdigit() for field in fields):
            raise ValueError('invalid namespace map')
        inside, outside, count = map(int, fields)
        if count <= 0 or inside + count > 2**32 - 1 or outside + count > 2**32 - 1:
            raise ValueError('invalid namespace map')
        rows.append((inside, outside, count))
    if not rows:
        raise ValueError('empty namespace map')
    return rows


def namespace_inventory(uid, gid, start, count, *, proc=Path('/proc')):
    """Best-effort snapshot, never proof that namespace mappings stay absent."""
    result = {'proc_view_identity_map_verified': False, 'host_proc_namespace_verified': False,
              'inspected': None, 'proc_pid1_namespace_skipped': None,
              'uid_overlaps': None, 'gid_overlaps': None, 'denied': None, 'exited': None,
              'invalid_or_changed': None, 'other_errors': None}
    try:
        initial = (proc / '1/ns/user').stat()
        own = (proc / 'self/ns/user').stat()
        # Maps are relative to the reader's user namespace. Even a full map
        # and PID1 match cannot establish host proc mount or visibility.
        full = [(0, 0, 2**32 - 1)]
        if ((initial.st_dev, initial.st_ino) != (own.st_dev, own.st_ino)
                or parse_id_map((proc / 'self/uid_map').read_text()) != full
                or parse_id_map((proc / 'self/gid_map').read_text()) != full):
            return result
        result['proc_view_identity_map_verified'] = True
        entries = [p for p in proc.iterdir() if p.name.isascii() and p.name.isdigit()]
        result.update(inspected=0, proc_pid1_namespace_skipped=0, uid_overlaps=0,
                      gid_overlaps=0, denied=0, exited=0, invalid_or_changed=0, other_errors=0)
    except (OSError, UnicodeError, ValueError):
        return result
    for entry in entries:
        try:
            before = (entry / 'ns/user').stat()
            identity = before.st_dev, before.st_ino
            if identity == (initial.st_dev, initial.st_ino):
                result['proc_pid1_namespace_skipped'] += 1
                continue
            uid_map = parse_id_map((entry / 'uid_map').read_text())
            gid_map = parse_id_map((entry / 'gid_map').read_text())
            after = (entry / 'ns/user').stat()
            if identity != (after.st_dev, after.st_ino):
                raise ValueError('namespace changed')
            for label, mapping, candidate in (('uid', uid_map, uid), ('gid', gid_map, gid)):
                if any((outside <= candidate < outside + size or
                        start < outside + size and outside < start + count)
                       for _, outside, size in mapping):
                    result[label + '_overlaps'] += 1
            result['inspected'] += 1
        except PermissionError:
            result['denied'] += 1
        except (FileNotFoundError, ProcessLookupError):
            result['exited'] += 1
        except (UnicodeError, ValueError):
            result['invalid_or_changed'] += 1
        except OSError:
            result['other_errors'] += 1
    return result


def subordinate_conflicts(text, start, count):
    """Return conflict count; malformed entries mean the inventory is unknown."""
    end = start + count
    conflicts = 0
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        fields = line.split(':')
        if (len(fields) != 3 or not fields[0] or not fields[1].isdigit()
                or not fields[2].isdigit() or int(fields[2]) <= 0):
            raise ValueError('malformed subordinate allocation')
        other, size = int(fields[1]), int(fields[2])
        if start < other + size and other < end:
            conflicts += 1
    return conflicts


def inspect_candidate(uid, gid, sub_start, sub_count, path, min_free):
    result = {'checks': {}, 'incomplete': [
        'physical_machine_hold_and_writer_coordination',
        'filesystem_quota_and_import_peak',
        'effective_service_cgroup_delegation_and_limits',
        'account_access_and_credential_authorization',
        'actual_user_namespace_and_daemon_isolation',
        'nss_enumeration_completeness_and_active_namespace_maps',
        'independent_host_proc_mount_and_namespace_visibility',
        'future_target_mount_identity_and_capacity',
    ]}
    checks = result['checks']
    checks['active_user_namespace_maps'] = namespace_inventory(uid, gid, sub_start, sub_count)
    checks['candidate_uid_in_subid_range'] = sub_start <= uid < sub_start + sub_count
    checks['candidate_gid_in_subid_range'] = sub_start <= gid < sub_start + sub_count
    try:
        try: pwd.getpwuid(uid); checks['uid_available'] = False
        except KeyError: checks['uid_available'] = True
        try: grp.getgrgid(gid); checks['gid_available'] = False
        except KeyError: checks['gid_available'] = True
        checks['subid_overlaps_account_uid'] = any(sub_start <= item.pw_uid < sub_start + sub_count
                                                   for item in pwd.getpwall())
        checks['subid_overlaps_account_gid'] = any(sub_start <= item.gr_gid < sub_start + sub_count
                                                   for item in grp.getgrall())
    except Exception:
        checks['uid_available'] = checks['gid_available'] = None
        checks['subid_overlaps_account_uid'] = checks['subid_overlaps_account_gid'] = None
    for label in ('subuid', 'subgid'):
        try:
            text = Path('/etc', label).read_text(encoding='utf-8')
            checks[label + '_conflicts'] = subordinate_conflicts(text, sub_start, sub_count)
            checks[label + '_candidate_id_conflicts'] = subordinate_conflicts(
                text, uid if label == 'subuid' else gid, 1)
        except (OSError, UnicodeError, ValueError):
            checks[label + '_conflicts'] = None
            checks[label + '_candidate_id_conflicts'] = None
    try:
        selected = Path(path)
        if not selected.is_absolute() or os.path.normpath(path) != path or path.startswith('//') or path == '/':
            raise ValueError()
        # No traversal through links, and no reads inside an existing candidate.
        current = Path('/')
        missing = False
        for part in selected.parts[1:]:
            current = current / part
            try: info = current.lstat()
            except FileNotFoundError:
                missing = True
                break
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ValueError()
        checks['candidate_absent'] = missing
        parent = current.parent if missing else current
        fs = os.statvfs(parent)
        checks['ancestor_available_bytes'] = fs.f_bavail * fs.f_frsize
        checks['ancestor_free_floor_met'] = checks['ancestor_available_bytes'] >= min_free
        checks['future_target_mount_verified'] = False
        checks['filesystem_quota_verified'] = False
    except (OSError, ValueError):
        checks.update(candidate_absent=None, ancestor_available_bytes=None, ancestor_free_floor_met=None,
                      filesystem_quota_verified=False, future_target_mount_verified=False)
    try:
        controllers = set(Path('/sys/fs/cgroup/cgroup.controllers').read_text().split())
        checks['cgroup_v2_controllers_present'] = sorted(controllers & {'cpu', 'memory', 'pids'})
    except (OSError, UnicodeError):
        checks['cgroup_v2_controllers_present'] = None
    # Readiness is deliberately never asserted: critical independent host and
    # machine-wide evidence above cannot be established from these observations.
    result['ready'] = False
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uid', type=int, required=True)
    parser.add_argument('--gid', type=int, required=True)
    parser.add_argument('--subid-start', type=int, required=True)
    parser.add_argument('--subid-count', type=int, required=True)
    parser.add_argument('--candidate-path', required=True)
    parser.add_argument('--min-free-bytes', type=int, required=True)
    args = parser.parse_args(argv)
    if (sys.platform != 'linux' or not 1 <= args.uid < 2**32 - 1
            or not 1 <= args.gid < 2**32 - 1 or not 1 <= args.subid_start < 2**32 - 1
            or not 1 <= args.subid_count <= 2**32 - 1 - args.subid_start
            or args.min_free_bytes <= 0):
        parser.error('Linux and valid positive candidate values are required')
    print(json.dumps(inspect_candidate(args.uid, args.gid, args.subid_start,
          args.subid_count, args.candidate_path, args.min_free_bytes), sort_keys=True))
    return 2  # incomplete evidence is not acceptance


if __name__ == '__main__':
    sys.exit(main())
