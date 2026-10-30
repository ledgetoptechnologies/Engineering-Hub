"""Discovery reader tests use only in-memory state and mocked HTTP."""

from datetime import datetime, timezone
import asyncio
import json
import sqlite3
from urllib.parse import parse_qs

import httpx
import pytest

from engineering_hub.infrastructure import InfrastructureRegistry
from engineering_hub.infrastructure_discovery_metrics import DiscoveryMetricsReader
from engineering_hub.infrastructure_metrics import QUERIES
from engineering_hub.infrastructure_prometheus import PrometheusOrigin, TARGET_PATH
from engineering_hub.infrastructure_service import InfrastructureService
from engineering_hub.repository import HubError


NOW = datetime(2026, 10, 29, 17, tzinfo=timezone.utc)


@pytest.fixture
def service():
    registry = InfrastructureRegistry(sqlite3.connect(':memory:', check_same_thread=False), capabilities={},
                                      clock=lambda: NOW)
    yield InfrastructureService(registry)
    registry.close()


def target(job='custom', instance='pc:9100', host='pc'):
    return {'labels': {'job': job, 'instance': instance, 'host': host}, 'health': 'up',
            'lastScrape': '2026-10-29T16:59:50Z', 'scrapeInterval': '15s',
            'scrapeUrl': 'http://never-follow.invalid/private'}


def handler(targets, *, evidence=(), fail=None, seen=None):
    queries = {query: (kind, key) for kind, key, query in QUERIES}
    def response(payload):
        return httpx.Response(200, headers={'content-type': 'application/json'},
                              stream=httpx.ByteStream(json.dumps(payload).encode()))
    def respond(request):
        if seen is not None:
            seen.append(request)
        assert request.url.host == '192.0.2.1'
        if request.url.raw_path.decode() == TARGET_PATH:
            return response({'status': 'success', 'data': {'activeTargets': targets}})
        kind, key = queries[parse_qs(request.url.query.decode())['query'][0]]
        if fail == (kind, key):
            return httpx.Response(302, headers={'location': 'http://never-follow.invalid/'})
        samples = [{'metric': {'job': 'custom', 'instance': 'pc:9100'}, 'value': [0, '1']}]
        return response({'status': 'success', 'data': {'resultType': 'vector',
                         'result': samples if (kind, key) in evidence else []}})
    return respond


def reader(service, transport, *, authorize=lambda actor: None, origin=True):
    return DiscoveryMetricsReader(service, PrometheusOrigin('192.0.2.1', 9090) if origin else None,
                                  authorize=authorize, inventory=lambda actor: [],
                                  test_transport=httpx.MockTransport(transport), clock=lambda: NOW)


@pytest.mark.asyncio
async def test_denied_before_io_and_revoked_after_io(service):
    seen = []
    def denied(actor):
        raise HubError('forbidden', 403)
    with pytest.raises(HubError):
        await reader(service, handler([], seen=seen), authorize=denied).read('agent')
    assert not seen
    calls = []
    def revoke(actor):
        calls.append(actor)
        if len(calls) == 2:
            raise HubError('forbidden', 403)
    with pytest.raises(HubError):
        await reader(service, handler([], seen=seen), authorize=revoke).read('agent')
    assert len(calls) == 2 and seen


@pytest.mark.asyncio
async def test_unconfigured_has_no_io(service):
    seen = []
    result = await reader(service, handler([], seen=seen), origin=False).read('agent')
    assert result['source'] == {'status': 'not_configured', 'truncated': False,
                                'omitted_count': 0, 'warnings': []}
    assert result['devices'] == [] and not seen


@pytest.mark.asyncio
async def test_unknown_inferred_and_conflict_is_unknown(service):
    seen = []
    result = await reader(service, handler([target()], evidence=[('node', 'mem_total')], seen=seen)).read('agent')
    row = result['devices'][0]
    assert row['exporter'] == 'node' and row['association'] == 'unregistered'
    assert row['state'] == 'fresh' and row['mem_total_bytes'] == 1
    assert result['source']['warnings'] == []
    assert len(seen) == 1 + len(QUERIES)
    assert 'never-follow' not in json.dumps(result)
    conflict = await reader(service, handler([target()], evidence=[('node', 'mem_total'),
                                                                    ('windows', 'mem_total')])).read('agent')
    assert conflict['devices'][0]['exporter'] is None
    assert conflict['source']['warnings'] == ['unknown_exporter']


@pytest.mark.asyncio
async def test_redirect_query_is_partial_and_not_followed(service):
    seen = []
    result = await reader(service, handler([target('node')], fail=('node', 'cpu'), seen=seen)).read('agent')
    assert result['source']['status'] == 'partial'
    assert result['source']['warnings'] == ['query_failed']
    assert all(request.url.host == '192.0.2.1' for request in seen)


@pytest.mark.asyncio
async def test_stale_series_without_active_target_cannot_create_a_machine(service):
    # Handler supplies a windows series under job=custom, while the only
    # active scrape has job=node. Metric evidence cannot create another row.
    result = await reader(service, handler([target('node')],
        evidence=[('windows', 'mem_total')])).read('agent')
    assert [(row['job'], row['instance']) for row in result['devices']] == [('node', 'pc:9100')]
    assert result['devices'][0]['exporter'] == 'node'


@pytest.mark.asyncio
async def test_cap_malformed_and_oversized_target_response(service):
    targets = [target('node', f'pc-{i:03}:9100') for i in range(205)] + [None]
    result = await reader(service, handler(targets)).read('agent')
    assert len(result['devices']) == 200 and result['source']['omitted_count'] == 5
    assert result['source']['truncated'] and result['source']['warnings'] == ['invalid_target', 'targets_truncated']
    def oversized(request):
        return httpx.Response(200, headers={'content-type': 'application/json'},
                              stream=httpx.ByteStream(b'x' * (1024 * 1024 + 1)))
    failure = await reader(service, oversized).read('agent')
    assert failure['source']['status'] == 'unreachable' and failure['devices'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('query_failure', ['timeout', 'budget'])
async def test_successful_target_discovery_survives_query_phase_failure(service, monkeypatch, query_failure):
    from engineering_hub import infrastructure_discovery_metrics as module
    from engineering_hub.infrastructure_metrics import _Exhausted
    target_parsed, first_query_completed = asyncio.Event(), asyncio.Event()
    parse_targets = module._discovery_targets

    def record_parsed_targets(raw):
        result = parse_targets(raw)
        target_parsed.set()
        return result

    monkeypatch.setattr(module, '_discovery_targets', record_parsed_targets)
    instance = reader(service, handler([target('node')]))

    async def unavailable_query(client, query, budget):
        await target_parsed.wait()
        if query == QUERIES[0][2]:
            first_query_completed.set()
            return [({'job': 'node', 'instance': 'pc:9100'}, 0.25)]
        await first_query_completed.wait()
        if query_failure == 'budget':
            raise _Exhausted()
        await asyncio.Event().wait()

    instance._query = unavailable_query
    monkeypatch.setattr(module, 'DEADLINE_SECONDS', 0.2)
    result = await instance.read('agent')
    assert result['source']['status'] == 'partial'
    assert result['source']['warnings'] == ['query_failed']
    assert [(row['job'], row['instance']) for row in result['devices']] == [('node', 'pc:9100')]
    assert result['devices'][0]['cpu_busy_ratio'] == 0.25
