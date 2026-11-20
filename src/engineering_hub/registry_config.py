"""Derive Hub registry wiring from the deployment's configured origin.

The registry is a built-in feature: configuring ``HUB_REGISTRY_ORIGIN`` turns
it on and registry events follow it. No separate enable switch is needed.
``HUB_REGISTRY_ENABLED`` and ``HUB_REGISTRY_EVENTS_ENABLED`` remain only as
explicit off switches ("0" or "false"); "1"/"true" is accepted but redundant.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

RegistryReason = Literal['origin configured', 'explicitly disabled', 'no origin']

_TRUE = frozenset({'1', 'true'})
_FALSE = frozenset({'0', 'false'})


@dataclass(frozen=True)
class RegistrySwitches:
    enabled: bool
    events_enabled: bool
    origin: str | None


def _explicit(environ: Mapping[str, str], name: str) -> bool | None:
    """Return the operator's explicit choice, or None when the flag is unset."""
    value = environ.get(name)
    if value is None:
        return None
    if value.lower() in _TRUE:
        return True
    if value.lower() in _FALSE:
        return False
    raise ValueError(f'{name} must be 0, 1, true or false (or unset)')


def registry_switches(environ: Mapping[str, str]) -> RegistrySwitches:
    """Registry is on when its origin is configured, unless explicitly disabled."""
    origin = environ.get('HUB_REGISTRY_ORIGIN')
    registry = _explicit(environ, 'HUB_REGISTRY_ENABLED')
    events = _explicit(environ, 'HUB_REGISTRY_EVENTS_ENABLED')
    if registry is True and origin is None:
        raise ValueError('HUB_REGISTRY_ENABLED requires HUB_REGISTRY_ORIGIN')
    enabled = origin is not None and registry is not False
    if events is True and not enabled:
        raise ValueError('HUB_REGISTRY_EVENTS_ENABLED requires an enabled registry')
    return RegistrySwitches(enabled, enabled and events is not False, origin)


def registry_reason(enabled: bool, origin: str | None) -> RegistryReason:
    """Explain why the registry is on or off, for startup status reporting."""
    if enabled:
        return 'origin configured'
    return 'no origin' if origin is None else 'explicitly disabled'
