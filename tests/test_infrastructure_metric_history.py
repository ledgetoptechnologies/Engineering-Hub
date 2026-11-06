"""History reads only what Prometheus's own target list names, for fixed ranges."""
from datetime import datetime, timezone
import json
import sqlite3
from urllib.parse import parse_qs

import httpx
import pytest

from engineering_hub.infrastructure import InfrastructureRegistry
from engineering_hub.infrastructure_discovery import normalize_targets
from engineering_hub.infrastructure_discovery_metrics import ALL_QUERIES, DiscoveryMetricsReader
from engineering_hub.infrastructure_metric_history import RANGES, _series_queries
from engineering_hub.infrastructure_prometheus import PrometheusOrigin, TARGET_PATH
from engineering_hub.infrastructure_service import InfrastructureService
from engineering_hub.private_gateway import browser_route_allowed
from engineering_hub.repository import HubError


NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
TARGETS = [
    {'labels': {'job': 'node', 'instance': 'nas:9100', 'host': 'nas'}, 'health': 'up',
     'lastScrape': '2026-11-06T11:59:50Z', 'scrapeInterval': '15s'},
    {'labels': {'job': 'windows', 'instance': 'desk:9182', 'host': 'desk'}, 'health': 'up',
     'lastScrape': '2026-11-06T11:59:50Z', 'scrapeInterval': '15s'},
]
INVENTORY = [{'device_id': 'nas', 'hostnames': ['nas']}]


def key_for(host):
    rows = normalize_targets(TARGETS, INVENTORY)['rows']
    return next(row['host_key'] for row in rows if row['host_label'] == host)


def json_response(payload):
    return httpx.Response(200, headers={'content-type': 'application/json'},
                          stream=httpx.ByteStream(json.dumps(payload).encode()))


def reader(respond, *, origin=True, authorize=lambda actor: None):
    registry = InfrastructureRegistry(sqlite3.connect(':memory:', check_same_thread=False), capabilities={},
                                      clock=lambda: NOW)
    return DiscoveryMetricsReader(InfrastructureService(registry),
                                  PrometheusOrigin('192.0.2.1', 9090) if origin else None,
                                  authorize=authorize, inventory=lambda actor: INVENTORY,
                                  test_transport=httpx.MockTransport(respond), clock=lambda: NOW)


def prometheus(values, calls=None):
    def respond(request):
        if request.url.raw_path.decode() == TARGET_PATH:
            return json_response({'status': 'success', 'data': {'activeTargets': TARGETS}})
        params = parse_qs(request.url.query.decode())
        assert request.url.path == '/api/v1/query_range'
        if calls is not None:
            calls.append(params)
        return json_response({'status': 'success', 'data': {'resultType': 'matrix', 'result': values(params['query'][0], params)}})
    return respond


@pytest.mark.asyncio
async def test_history_keeps_gaps_as_null_and_splits_disks_by_mount():
    calls = []

    def values(query, params):
        start = int(params['start'][0])
        if 'node_cpu_seconds_total' in query:
            return [{'metric': {}, 'values': [[start, '0.5'], [start + 60, '2']]}]
        if 'node_filesystem' in query:
            return [{'metric': {'mountpoint': '/data'}, 'values': [[start, '0.8']]},
                    {'metric': {'mountpoint': '/'}, 'values': [[start, '0.1']]}]
        return []

    result = await reader(prometheus(values, calls)).history('internal', key_for('nas'), '1h')
    assert result['source'] == {'status': 'ok'} and result['step_seconds'] == 30
    cpu = next(item for item in result['series'] if item['key'] == 'cpu')
    assert len(cpu['points']) == 121
    assert cpu['points'][0][1] == 0.5
    assert cpu['points'][1][1] is None, 'a missing sample is a gap, never zero'
    assert cpu['points'][2][1] is None, 'an impossible ratio is dropped'
    assert [item['label'] for item in result['series'] if item['key'] == 'disk'] == ['/', '/data']
    assert all('job="node",instance="nas:9100"' in call['query'][0] for call in calls)
    assert all('desk' not in call['query'][0] for call in calls)
    assert {call['step'][0] for call in calls} == {'30'}


@pytest.mark.asyncio
async def test_unknown_machine_and_bad_input_are_refused_before_any_range_query():
    calls = []
    source = reader(prometheus(lambda *_: [], calls))
    with pytest.raises(HubError) as missing:
        await source.history('internal', 'host:' + 'a' * 64, '1h')
    assert missing.value.status == 404 and not calls
    for host, span in (('nas', '1h'), (key_for('nas'), '30d'), (key_for('nas') + '"} or up{', '1h')):
        with pytest.raises(HubError) as bad:
            await source.history('internal', host, span)
        assert bad.value.code == 'invalid_infrastructure_filter'


@pytest.mark.asyncio
async def test_one_failed_range_query_is_partial_and_no_targets_is_unreachable():
    def respond(request):
        if request.url.raw_path.decode() == TARGET_PATH:
            return json_response({'status': 'success', 'data': {'activeTargets': TARGETS}})
        if 'node_cpu_seconds_total' in parse_qs(request.url.query.decode())['query'][0]:
            return httpx.Response(503)
        return json_response({'status': 'success', 'data': {'resultType': 'matrix', 'result': []}})

    assert (await reader(respond).history('internal', key_for('nas'), '6h'))['source']['status'] == 'partial'
    down = await reader(lambda request: httpx.Response(503)).history('internal', key_for('nas'), '6h')
    assert down['source']['status'] == 'unreachable' and down['series'] == []
    unset = await reader(prometheus(lambda *_: []), origin=False).history('internal', key_for('nas'), '6h')
    assert unset['source']['status'] == 'not_configured'


@pytest.mark.asyncio
async def test_access_is_rechecked_after_the_network():
    state = {'calls': 0}

    def authorize(actor):
        state['calls'] += 1
        if state['calls'] > 1:
            raise HubError('forbidden', 403)

    with pytest.raises(HubError) as refused:
        await reader(prometheus(lambda *_: []), authorize=authorize).history('internal', key_for('nas'), '1h')
    assert refused.value.status == 403


def test_label_values_cannot_leave_their_string():
    queries = _series_queries('node', 'a"b', 'c\\d', '5m')
    assert all('job="a\\"b",instance="c\\\\d"' in item[3] for item in queries)


def test_every_range_stays_small_and_network_queries_are_fixed_text():
    assert all(seconds // step <= 200 for seconds, step, _ in RANGES.values())
    assert len({query for _, _, query in ALL_QUERIES}) == len(ALL_QUERIES)


def test_gateway_exposes_the_read_only():
    path = '/api/hub/v1/infrastructure/metrics/history'
    assert browser_route_allowed('GET', path)
    assert not browser_route_allowed('POST', path)
