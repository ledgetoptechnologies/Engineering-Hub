"""Closed data-only configuration for opt-in installed bound journal use.

The containing runner config must be independently reviewed and protected by
the existing read-only mount checks. This parser does not create authority.
"""
from dataclasses import dataclass, fields, replace
import re
from uuid import UUID

from engineering_hub.current_executor_observation import CurrentLaunchPins
from .effect_binding_canonical import NonsecretEffectBinding, canonical_nonsecret_binding
from .enrolled_effect_provider import DeferredTargetEffect


@dataclass(frozen=True)
class InstalledBoundProfile:
    key_id: str
    launch: CurrentLaunchPins
    effects: tuple


def parse_bound_profile(value):
    if type(value) is not dict or set(value) != {'key_id', 'launch', 'effects'}:
        raise ValueError('closed bound profile required')
    if type(value['key_id']) is not str or not re.fullmatch(r'sha256:[a-f0-9]{64}', value['key_id']):
        raise ValueError('independent key identity required')
    launch_data = value['launch']
    if type(launch_data) is not dict or set(launch_data) != {f.name for f in fields(CurrentLaunchPins)}:
        raise ValueError('independent launch pins required')
    launch = CurrentLaunchPins(**launch_data)
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
    return InstalledBoundProfile(value['key_id'], launch, tuple(result))
