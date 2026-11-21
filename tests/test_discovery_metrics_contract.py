"""The actual discovery reader must satisfy the browser's schema-3 invariants."""

from datetime import datetime, timezone
import json
import re
import sqlite3
from urllib.parse import parse_qs

import httpx
import pytest

from engineering_hub.infrastructure import InfrastructureRegistry
from engineering_hub.infrastructure_discovery_metrics import DiscoveryMetricsReader
from engineering_hub.infrastructure_discovery_metrics import ALL_QUERIES as QUERIES
from engineering_hub.infrastructure_prometheus import PrometheusOrigin, TARGET_PATH
from engineering_hub.infrastructure_service import InfrastructureService


NOW = datetime(2026, 10, 29, 17, tzinfo=timezone.utc)
WARNINGS = {'invalid_target', 'ambiguous_inventory', 'unknown_exporter',
            'targets_truncated', 'series_truncated', 'query_failed'}


def assert_browser_contract(result):
    """Check cross-field constraints independently of the reader implementation."""
    assert set(result) == {'schema_version', 'generated_at', 'source', 'devices'}
    assert result['schema_version'] == 3
    source, rows = result['source'], result['devices']
    assert set(source) == {'status', 'truncated', 'omitted_count', 'warnings'}
    assert source['status'] in {'ok', 'partial', 'unreachable', 'not_configured'}
    assert source['warnings'] == sorted(set(source['warnings']))
    assert set(source['warnings']) <= WARNINGS
    assert source['truncated'] or source['omitted_count'] == 0
    assert len(rows) <= 200
    if source['status'] in {'unreachable', 'not_configured'}:
        assert not rows
    assert len({(row['job'], row['instance']) for row in rows}) == len(rows)
    for row in rows:
        assert bool(re.fullmatch(r'(host|target):[0-9a-f]{64}', row['host_key']))
        assert row['host_key'].startswith('target:') == (row['host_label'] is None)
        assert row['association'] in {'matched', 'unregistered', 'ambiguous'}
        slug = isinstance(row['device_id'], str) and bool(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', row['device_id']))
        assert (row['association'] == 'matched') == slug
        if row['association'] != 'matched':
            assert row['device_id'] is None
        assert row['state'] in {'fresh', 'stale', 'unknown'}
        if row['state'] in {'fresh', 'stale'}:
            assert row['last_scrape'] is not None
        if row['mem_available_bytes'] is not None:
            assert row['mem_total_bytes'] is not None
            assert row['mem_available_bytes'] <= row['mem_total_bytes']
        if row['cpu_busy_ratio'] is not None:
            assert 0 <= row['cpu_busy_ratio'] <= 1
        for disk in row['disks']:
            if disk['free_bytes'] is not None:
                assert disk['free_bytes'] <= disk['size_bytes']
        for gpu in row['gpus']:
            if gpu['util_ratio'] is not None:
                assert 0 <= gpu['util_ratio'] <= 1


def target(job, instance, host):
    return {'labels': {'job': job, 'instance': instance, 'host': host}, 'health': 'up',
            'lastScrape': '2026-10-29T16:59:50Z', 'scrapeInterval': '15s'}


@pytest.mark.asyncio
async def test_reader_result_satisfies_browser_contract_with_partial_query():
    registry = InfrastructureRegistry(sqlite3.connect(':memory:', check_same_thread=False), capabilities={},
                                      clock=lambda: NOW)
    service = InfrastructureService(registry)
    targets = [target('windows', 'desk:9182', 'desk'),
               target('nvidia-gpu', 'desk:9400', 'desk'),
               target('node', 'unknown:9100', '🛰' * 40),
               target('node', 'shared:9100', 'shared')]
    inventory = [{'device_id': 'desk', 'hostnames': ['desk']},
                 {'device_id': 'first', 'hostnames': ['shared']},
                 {'device_id': 'second', 'hostnames': ['shared']}]
    queries = {query: (kind, key) for kind, key, query in QUERIES}

    def respond(request):
        assert request.url.host == '192.0.2.1'
        if request.url.raw_path.decode() == TARGET_PATH:
            payload = {'status': 'success', 'data': {'activeTargets': targets}}
        else:
            kind, key = queries[parse_qs(request.url.query.decode())['query'][0]]
            if (kind, key) == ('node', 'uptime'):
                return httpx.Response(503)
            values = {
                ('windows', 'cpu'): ('windows', 'desk:9182', '0.25', {}),
                ('windows', 'mem_total'): ('windows', 'desk:9182', '1000', {}),
                ('windows', 'mem_available'): ('windows', 'desk:9182', '400', {}),
                ('windows', 'disk_size'): ('windows', 'desk:9182', '2000', {'volume': 'C:'}),
                ('windows', 'disk_free'): ('windows', 'desk:9182', '500', {'volume': 'C:'}),
                ('nvidia', 'gpu_util'): ('nvidia-gpu', 'desk:9400', '50', {'gpu': '0'}),
            }
            value = values.get((kind, key))
            samples = [] if value is None else [{'metric': {'job': value[0], 'instance': value[1], **value[3]},
                                                 'value': [NOW.timestamp(), value[2]]}]
            payload = {'status': 'success', 'data': {'resultType': 'vector', 'result': samples}}
        return httpx.Response(200, headers={'content-type': 'application/json'},
                              stream=httpx.ByteStream(json.dumps(payload).encode()))

    try:
        reader = DiscoveryMetricsReader(service, PrometheusOrigin('192.0.2.1', 9090),
            authorize=lambda actor: None, inventory=lambda actor: inventory,
            test_transport=httpx.MockTransport(respond), clock=lambda: NOW)
        result = await reader.read('internal')
        assert_browser_contract(result)
        assert result['source']['status'] == 'partial'
        assert result['source']['warnings'] == ['ambiguous_inventory', 'query_failed']
        rows = {row['instance']: row for row in result['devices']}
        assert rows['desk:9182']['association'] == rows['desk:9400']['association'] == 'matched'
        assert rows['desk:9182']['exporter'] == 'windows' and rows['desk:9182']['disks']
        assert rows['desk:9400']['exporter'] == 'nvidia' and rows['desk:9400']['gpus']
        assert rows['unknown:9100']['association'] == 'unregistered'
        assert rows['unknown:9100']['host_label'] == '🛰' * 40
        assert rows['shared:9100']['association'] == 'ambiguous'
    finally:
        registry.close()
