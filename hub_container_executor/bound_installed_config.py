"""Closed data-only configuration for opt-in installed bound journal use.

The containing runner config must be independently reviewed and protected by
the existing read-only mount checks. This parser does not create authority.
"""
from dataclasses import dataclass, fields, replace
import re
from uuid import UUID

from .effect_binding_canonical import NonsecretEffectBinding, canonical_nonsecret_binding
from .enrolled_effect_provider import DeferredTargetEffect


@dataclass(frozen=True)
class InstalledBoundProfile:
    key_id: str
    approval_path: str
    approver_public_key: bytes
    approval_wait_ms: int
    effects: tuple
    app_cgroup: object = None


def parse_bound_profile(value):
    if (type(value) is not dict or set(value) not in (
            {'key_id', 'approval_path', 'approver_public_key_hex', 'approval_wait_ms', 'effects'},
            {'key_id', 'approval_path', 'approver_public_key_hex', 'approval_wait_ms', 'effects', 'app_cgroup'})):
        raise ValueError('closed bound profile required')
    if type(value['key_id']) is not str or not re.fullmatch(r'sha256:[a-f0-9]{64}', value['key_id']):
        raise ValueError('independent key identity required')
    import os
    path, public_hex, wait_ms = value['approval_path'], value['approver_public_key_hex'], value['approval_wait_ms']
    if (type(path) is not str or not path.startswith('/') or path.startswith('//') or path == '/'
            or os.path.normpath(path) != path or '\x00' in path
            or type(public_hex) is not str or not re.fullmatch(r'[a-f0-9]{64}', public_hex)
            or type(wait_ms) is not int or not 100 <= wait_ms <= 3600000):
        raise ValueError('protected postlaunch approval admission required')
    rows = value['effects']
    if type(rows) is not list or not 1 <= len(rows) <= 128:
        raise ValueError('bounded effect enrollment required')
    names = {f.name for f in fields(NonsecretEffectBinding)} - {'owner_epoch'}
    result = []
    for row in rows:
        if type(row) is not dict or set(row) != {'binding', 'create_command_id'}:
            raise ValueError('closed effect enrollment required')
        data = row['binding']
        if type(data) is not dict or set(data) != names or type(data['environment_names']) is not list:
            raise ValueError('closed effect binding required')
        # Owner epoch is assigned solely by the acquired journal, not by the
        # configuration. This inert placeholder is never issued or MACed.
        effect = NonsecretEffectBinding(**(data | {'owner_epoch': '00000000-0000-4000-8000-000000000000',
                                                      'environment_names': tuple(data['environment_names'])}))
        origin = row['create_command_id']
        if origin is None:
            canonical_nonsecret_binding(effect)
            result.append(effect)
        else:
            if (type(origin) is not str or str(UUID(origin)) != origin
                    or effect.kind not in {'start', 'stop', 'probe'} or effect.container_id is not None):
                raise ValueError('deferred target required')
            # Actual container ID is validated only once observed and bound;
            # the placeholder is used for syntax checking and never retained.
            canonical_nonsecret_binding(replace(effect, container_id='0' * 64))
            result.append(DeferredTargetEffect(effect, origin))
    if len({(x.template if type(x) is DeferredTargetEffect else x).operation_id for x in result}) != len(result):
        raise ValueError('duplicate effect identity')
    app_cgroup = None
    if 'app_cgroup' in value:
        from .app_cgroup_observation import AppCgroupPins
        item = value['app_cgroup']
        if type(item) is not dict or set(item) != {'parent', 'device', 'inode', 'namespace_device', 'namespace_inode'}:
            raise ValueError('closed app cgroup pins required')
        app_cgroup = AppCgroupPins(**item)
    return InstalledBoundProfile(value['key_id'], path, bytes.fromhex(public_hex), wait_ms, tuple(result), app_cgroup)
