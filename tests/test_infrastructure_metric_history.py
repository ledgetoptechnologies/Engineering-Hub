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


def reader(respond, *, origin=True, authorize=lambda actor: None, now=NOW):
    registry = InfrastructureRegistry(sqlite3.connect(':memory:', check_same_thread=False), capabilities={},
                                       clock=lambda: now)
    return DiscoveryMetricsReader(InfrastructureService(registry),
                                  PrometheusOrigin('192.0.2.1', 9090) if origin else None,
                                  authorize=authorize, inventory=lambda actor: INVENTORY,
                                  test_transport=httpx.MockTransport(respond), clock=lambda: now)


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
async def test_history_with_more_disks_than_the_cap_keeps_the_fullest():
    from engineering_hub.infrastructure_metrics import MAX_DISKS
    names = [f'/m{index:02d}' for index in range(MAX_DISKS + 1)]

    def values(query, params):
        start = int(params['start'][0])
        if 'node_filesystem' in query:
            # The last mount by name is the fullest by its latest reading.
            return [{'metric': {'mountpoint': name},
                     'values': [[start, '0.1'], [start + 30, '0.97' if name == names[-1] else '0.2']]} for name in names]
        return []

    result = await reader(prometheus(values, [])).history('internal', key_for('nas'), '1h')
    labels = [item['label'] for item in result['series'] if item['key'] == 'disk']
    assert len(labels) == MAX_DISKS and names[-1] in labels and labels == sorted(labels)


@pytest.mark.asyncio
async def test_unknown_machine_and_bad_input_are_refused_before_any_range_query():
    calls = []
    source = reader(prometheus(lambda *_: [], calls))
    with pytest.raises(HubError) as missing:
        await source.history('internal', 'host:' + 'a' * 64, '1h')
    assert missing.value.status == 404 and not calls
    for host, span in (('nas', '1h'), (key_for('nas'), '31d'), (key_for('nas') + '"} or up{', '1h')):
        with pytest.raises(HubError) as bad:
            await source.history('internal', host, span)
        assert bad.value.code == 'invalid_infrastructure_filter'


@pytest.mark.asyncio
async def test_thirty_day_history_covers_full_window_without_increasing_sample_count():
    calls = []

    def values(query, params):
        if 'node_cpu_seconds_total' not in query:
            return []
        return [{'metric': {}, 'values': [[int(params['start'][0]), '0.23'], [int(params['end'][0]), '0.42']]}]

    now = NOW.replace(hour=15, minute=37)
    result = await reader(prometheus(values, calls), now=now).history('internal', key_for('nas'), '30d')
    assert result['range'] == '30d' and result['step_seconds'] == 21600
    assert result['source'] == {'status': 'ok'}
    points = result['series'][0]['points']
    assert len(points) == 121 and points[-1][0] - points[0][0] == 30 * 86400
    assert points[-1][0] == int(now.replace(minute=0).timestamp())
    assert points[0][1] == 0.23 and points[-1][1] == 0.42 and points[1][1] is None
    assert len(calls) == 5 and {call['step'][0] for call in calls} == {'21600'}
    assert all('job="node",instance="nas:9100"' in call['query'][0] for call in calls)


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


@pytest.mark.asyncio
async def test_more_targets_than_charted_for_one_host_is_partial_and_stays_bounded():
    from engineering_hub.infrastructure_metric_history import MAX_TARGETS_PER_HOST
    many = [{'labels': {'job': f'node{index}' if index else 'node', 'instance': f'nas:{9100 + index}', 'host': 'nas'},
             'health': 'up', 'lastScrape': '2026-11-06T11:59:50Z', 'scrapeInterval': '15s'}
            for index in range(MAX_TARGETS_PER_HOST + 1)]
    calls = []

    def respond(request):
        if request.url.raw_path.decode() == TARGET_PATH:
            return json_response({'status': 'success', 'data': {'activeTargets': many}})
        calls.append(parse_qs(request.url.query.decode())['query'][0])
        return json_response({'status': 'success', 'data': {'resultType': 'matrix', 'result': []}})

    key = next(row['host_key'] for row in normalize_targets(many, INVENTORY)['rows'] if row['host_label'] == 'nas')
    result = await reader(respond).history('internal', key, '1h')
    assert result['source']['status'] == 'partial'
    # The fifth target is never queried: the bound holds, and the answer says something was left out.
    assert not any(f'nas:{9100 + MAX_TARGETS_PER_HOST}' in query for query in calls)
    assert any(f'nas:{9100 + MAX_TARGETS_PER_HOST - 1}' in query for query in calls)

    exact = many[:MAX_TARGETS_PER_HOST]

    def respond_exact(request):
        if request.url.raw_path.decode() == TARGET_PATH:
            return json_response({'status': 'success', 'data': {'activeTargets': exact}})
        return json_response({'status': 'success', 'data': {'resultType': 'matrix', 'result': []}})

    assert (await reader(respond_exact).history('internal', key, '1h'))['source']['status'] == 'ok'


@pytest.mark.asyncio
async def test_dcgm_exporter_history_is_split_per_gpu_with_units():
    gpu_targets = TARGETS + [{'labels': {'job': 'nvidia-gpu', 'instance': 'nas:9400', 'host': 'nas'}, 'health': 'up',
                              'lastScrape': '2026-11-06T11:59:50Z', 'scrapeInterval': '15s'}]
    calls = []

    def respond(request):
        if request.url.raw_path.decode() == TARGET_PATH:
            return json_response({'status': 'success', 'data': {'activeTargets': gpu_targets}})
        params = parse_qs(request.url.query.decode())
        query, start = params['query'][0], int(params['start'][0])
        calls.append(query)
        values = {'GPU_UTIL': ('0.5', '0.25'), 'FB_USED': ('1024', '2048'), 'GPU_TEMP': ('45', '54'),
                  'POWER_USAGE': ('20.5', '31'), 'SM_CLOCK': ('1500000000', '210000000')}
        found = next((pair for name, pair in values.items() if name in query), None)
        result = [] if found is None else [{'metric': {'gpu': gpu}, 'values': [[start, value]]}
                                           for gpu, value in zip(('0', '1'), found)]
        return json_response({'status': 'success', 'data': {'resultType': 'matrix', 'result': result}})

    key = next(row['host_key'] for row in normalize_targets(gpu_targets, INVENTORY)['rows'] if row['host_label'] == 'nas')
    result = await reader(respond).history('internal', key, '1h')
    assert result['source']['status'] == 'ok'
    gpu = {(item['key'], item['label']): (item['unit'], item['points'][0][1]) for item in result['series'] if item['key'].startswith('gpu_')}
    assert gpu == {
        ('gpu_util', '0'): ('ratio', 0.5), ('gpu_util', '1'): ('ratio', 0.25),
        ('gpu_memory', '0'): ('bytes', 1024), ('gpu_memory', '1'): ('bytes', 2048),
        ('gpu_temperature', '0'): ('celsius', 45), ('gpu_temperature', '1'): ('celsius', 54),
        ('gpu_power', '0'): ('watts', 20.5), ('gpu_power', '1'): ('watts', 31),
        ('gpu_clock', '0'): ('hertz', 1500000000), ('gpu_clock', '1'): ('hertz', 210000000),
    }
    dcgm = [query for query in calls if 'DCGM_' in query]
    assert len(dcgm) == 5 and all('job="nvidia-gpu",instance="nas:9400"' in query for query in dcgm)
