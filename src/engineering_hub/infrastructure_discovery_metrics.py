"""On-demand discovery metrics, with caller-supplied admission and inventory.

Target labels are untrusted display hints, never network destinations or grants.
The callbacks are synchronous; authorization is checked before I/O and again
after the last await. The caller owns session revocation and inventory policy.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import inspect

import httpx

from .infrastructure_discovery import MAX_INPUT_TARGETS, normalize_targets
from .infrastructure_metrics import (DEADLINE_SECONDS, QUERIES, MetricsReader,
                                     _Budget, _Exhausted, _iso)
from .infrastructure_prometheus import PrometheusOrigin, TARGET_PATH, _need, _unique
from .infrastructure_service import InfrastructureService


def _discovery_targets(raw):
    def finite(value):
        number = float(value)
        _need(math.isfinite(number))
        return number
    value = json.loads(raw, object_pairs_hook=_unique, parse_float=finite,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    _need(type(value) is dict and value.get('status') == 'success'
          and type(value.get('data')) is dict)
    targets = value['data'].get('activeTargets')
    _need(type(targets) is list and len(targets) <= MAX_INPUT_TARGETS)
    return targets


@dataclass(frozen=True)
class _Identity:
    job: str
    instance: str
    device_id: str | None
    probe_id: str | None
    exporter: str | None


class DiscoveryMetricsReader(MetricsReader):
    """Schema 2 read-only discovery using the fixed legacy query definitions."""

    def __init__(self, service, origin, *, authorize, inventory, test_transport=None, clock=None):
        _need(type(service) is InfrastructureService)
        _need(origin is None or type(origin) is PrometheusOrigin)
        _need(callable(authorize) and callable(inventory)
              and not inspect.iscoroutinefunction(authorize)
              and not inspect.iscoroutinefunction(inventory))
        _need(test_transport is None or type(test_transport) is httpx.MockTransport)
        self.service, self.origin, self._transport = service, origin, test_transport
        self._authorize, self._inventory = authorize, inventory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _admit(self, actor):
        result = self._authorize(actor)
        # Admission callbacks return None on success and raise on denial. Fail
        # closed if a wrapper accidentally returns False or an unawaited task.
        if inspect.iscoroutine(result):
            result.close()
        _need(result is None)

    async def read(self, actor):
        self._admit(actor)
        if self.origin is None:
            return self._envelope('not_configured', [], [], 0)
        budget = _Budget()
        targets, results = None, {}
        try:
            async with asyncio.timeout(DEADLINE_SECONDS):
                async with httpx.AsyncClient(
                    trust_env=False, follow_redirects=False, transport=self._transport,
                    limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
                    timeout=httpx.Timeout(5, connect=2, write=2, pool=2),
                    headers={'Accept': 'application/json', 'Accept-Encoding': 'identity'},
                ) as client:
                    async def fetch_targets():
                        try:
                            return _discovery_targets(await self._get(client, TARGET_PATH, budget))
                        except (httpx.HTTPError, ValueError, TypeError, RecursionError, UnicodeError):
                            return None
                    exhausted = False
                    try:
                        async with asyncio.TaskGroup() as group:
                            pending = [group.create_task(fetch_targets())] + [
                                group.create_task(self._query(client, query, budget))
                                for _, _, query in QUERIES]
                    except* _Exhausted:
                        exhausted = True
            if not exhausted:
                targets = pending[0].result()
                results = {(kind, key): task.result()
                           for (kind, key, _), task in zip(QUERIES, pending[1:])}
        except TimeoutError:
            pass
        self._admit(actor)
        if targets is None:
            return self._envelope('unreachable', [], [], 0)
        failed = len(results) != len(QUERIES) or any(value is None for value in results.values())
        inventory = self._inventory(actor)
        if inspect.iscoroutine(inventory):
            inventory.close()
        _need(type(inventory) is list)
        normalized = normalize_targets(targets, inventory)
        warnings = set(normalized['warnings']) - {'unknown_exporter'}
        if failed:
            warnings.add('query_failed')
        evidence = {}
        for (kind, _), samples in results.items():
            for labels, value in samples or ():
                job, instance = labels.get('job'), labels.get('instance')
                if value is not None and type(job) is str and type(instance) is str:
                    evidence.setdefault((job, instance), set()).add(kind)
        devices = []
        for row in normalized['rows']:
            kinds = set(evidence.get((row['job'], row['instance']), ()))
            if row['exporter'] is not None:
                kinds.add(row['exporter'])
            exporter = next(iter(kinds)) if len(kinds) == 1 else None
            if exporter is None:
                warnings.add('unknown_exporter')
            identity = _Identity(row['job'], row['instance'], row['device_id'], None, exporter)
            device = self._device(identity, [target for target in targets if type(target) is dict],
                                  results, self._clock())
            device.update(row)
            device['exporter'] = exporter
            devices.append(device)
        self._admit(actor)
        return self._envelope('partial' if failed else 'ok', devices, warnings,
                              normalized['omitted_count'])

    def _envelope(self, status, devices, warnings, omitted):
        return {'schema_version': 2, 'generated_at': _iso(self._clock()),
                'source': {'status': status, 'truncated': omitted > 0,
                           'omitted_count': omitted, 'warnings': sorted(warnings)},
                'devices': devices}
