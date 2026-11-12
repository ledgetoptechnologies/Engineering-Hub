"""Root-protected, independently supplied daemon scope observation policy.

This is a constraint on an observation only. It does not stop a daemon, attest
that an effect completed, authorize a retry or clear a recovery hold.
"""
from dataclasses import dataclass
import os
import re
from uuid import UUID
from .launch_supervisor import _boot_id, _path, _root_file, _strict


class DaemonScopePolicyError(ValueError):
    pass


@dataclass(frozen=True)
class DaemonScopePolicy:
    path: object
    file_identity: object
    scope: str
    scope_device: int
    scope_inode: int
    daemon_pid: int
    daemon_start_ticks: int
    daemon_id: str
    boot_id: str
    wait_seconds: int
    members: tuple = ()

    def check(self, supervisor, scope=None):
        try:
            _, current = _root_file(self.path, 16384)
            pin, process = supervisor._daemon_pin, supervisor._engine.process
            if (current != self.file_identity or _boot_id() != self.boot_id
                    or pin.pid != self.daemon_pid or pin.start_ticks != self.daemon_start_ticks
                    or pin.daemon_id != self.daemon_id or process.pid != pin.pid
                    or process.start != pin.start_ticks or process.boot != self.boot_id):
                raise ValueError()
            if scope is not None and (scope._scope != self.scope or scope._identity != (self.scope_device, self.scope_inode)
                                      or not any(item is process for item in scope._processes)
                                      or scope._allow_descendants is not bool(self.members)
                                      or (self.members and scope._original != self.members)):
                raise ValueError()
        except Exception:
            raise DaemonScopePolicyError('daemon scope policy unavailable') from None


def load_policy(path):
    try:
        if os.getuid() != 0 or os.geteuid() != 0:
            raise ValueError()
        path = _path(path)
        raw, identity = _root_file(path, 16384)
        data = _strict(raw, 16384)
        fields = {'schema', 'scope', 'scope_device', 'scope_inode', 'daemon_pid',
                  'daemon_start_ticks', 'daemon_id', 'boot_id', 'wait_seconds'}
        if type(data) is not dict:
            raise ValueError()
        schema = data.get('schema')
        if not ((schema == 'engineering-hub.daemon-scope-policy.v1' and set(data) == fields)
                or (schema == 'engineering-hub.daemon-scope-policy.v2' and set(data) == fields | {'members'})):
            raise ValueError()
        scope = data['scope']
        if (type(scope) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', scope)
                or any(item in ('.', '..') for item in scope.split('/'))):
            raise ValueError()
        for key in ('scope_device', 'scope_inode', 'daemon_pid', 'daemon_start_ticks', 'wait_seconds'):
            if type(data[key]) is not int or not 1 <= data[key] < 2**63:
                raise ValueError()
        if data['daemon_pid'] <= 1 or data['wait_seconds'] > 3600:
            raise ValueError()
        if (type(data['daemon_id']) is not str or not re.fullmatch(r'[A-Za-z0-9:_.-]{1,128}', data['daemon_id'])
                or type(data['boot_id']) is not str or str(UUID(data['boot_id'])) != data['boot_id']):
            raise ValueError()
        members = ()
        if schema == 'engineering-hub.daemon-scope-policy.v2':
            raw_members = data['members']
            if type(raw_members) is not list or not 1 <= len(raw_members) <= 128:
                raise ValueError()
            parsed = []
            for item in raw_members:
                if (type(item) is not dict or set(item) != {'pid', 'start_ticks'}
                        or type(item['pid']) is not int or not 1 < item['pid'] < 2**31
                        or type(item['start_ticks']) is not int or not 1 <= item['start_ticks'] < 2**63):
                    raise ValueError()
                parsed.append((item['pid'], item['start_ticks']))
            if len({pid for pid, _ in parsed}) != len(parsed):
                raise ValueError()
            members = tuple(sorted(parsed))
            if (data['daemon_pid'], data['daemon_start_ticks']) not in members:
                raise ValueError()
        return DaemonScopePolicy(path, identity, scope, data['scope_device'], data['scope_inode'],
            data['daemon_pid'], data['daemon_start_ticks'], data['daemon_id'], data['boot_id'], data['wait_seconds'], members)
    except Exception:
        raise DaemonScopePolicyError('daemon scope policy unavailable') from None
