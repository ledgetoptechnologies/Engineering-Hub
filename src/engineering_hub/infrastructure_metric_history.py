"""Fixed-range history for one discovered machine, read on demand from Prometheus.

The browser names only an opaque host key and one of four fixed ranges. The key
is matched against Prometheus's own active targets, and the job and instance
used in the queries come from that list, never from the request. Nothing is
stored, scheduled or cached. Missing samples stay null so a gap reads as a gap.
"""
import asyncio
import json
import math
import re
from urllib.parse import urlencode

import httpx

from .infrastructure_discovery import normalize_targets
from .infrastructure_metrics import (DEADLINE_SECONDS, MAX_DISKS, _NODE_FS, _Budget, _Exhausted,
                                     _iso, _number, _plain)
from .infrastructure_prometheus import TARGET_PATH, _need
from .repository import HubError


SCHEMA_VERSION = 1
# Range name -> (seconds shown, seconds between points, rate window).
RANGES = {'1h': (3600, 30, '5m'), '6h': (21600, 180, '5m'),
          '24h': (86400, 720, '12m'), '7d': (604800, 3600, '1h')}
MAX_TARGETS_PER_HOST = 4
MAX_RESULT_SERIES = 32
_HOST_KEY = re.compile(r'(?:host|target):[0-9a-f]{64}')
_NODE_NIC = 'device!~"lo|veth.*|docker.*|br-.*|virbr.*|vnet.*|tap.*|cali.*|flannel.*|cni.*"'
_WINDOWS_NIC = 'nic!~".*[Ll]oopback.*"'
_WINDOWS_DISK = 'volume=~"[A-Z]:"'

# Current network rate for the summary. Fixed text only, like QUERIES.
NETWORK_QUERIES = (
    ('node', 'net_rx', f'sum by (job,instance) (rate(node_network_receive_bytes_total{{{_NODE_NIC}}}[5m]))'),
    ('node', 'net_tx', f'sum by (job,instance) (rate(node_network_transmit_bytes_total{{{_NODE_NIC}}}[5m]))'),
    ('windows', 'net_rx', f'sum by (job,instance) (rate(windows_net_bytes_received_total{{{_WINDOWS_NIC}}}[5m]))'),
    ('windows', 'net_tx', f'sum by (job,instance) (rate(windows_net_bytes_sent_total{{{_WINDOWS_NIC}}}[5m]))'),
)


def _label(value):
    return value.replace('\\', '\\\\').replace('"', '\\"')


def _series_queries(exporter, job, instance, window):
    """(key, unit, label name, PromQL) for one target; values come from Prometheus's target list."""
    target = f'job="{_label(job)}",instance="{_label(instance)}"'
    if exporter == 'node':
        return (
            ('cpu', 'ratio', None, f'1 - avg(rate(node_cpu_seconds_total{{{target},mode="idle"}}[{window}]))'),
            ('memory', 'ratio', None, f'1 - sum(node_memory_MemAvailable_bytes{{{target}}}) / sum(node_memory_MemTotal_bytes{{{target}}})'),
            ('disk', 'ratio', 'mountpoint',
             f'1 - max by (mountpoint) (node_filesystem_avail_bytes{{{target},{_NODE_FS}}})'
             f' / max by (mountpoint) (node_filesystem_size_bytes{{{target},{_NODE_FS}}})'),
            ('network_rx', 'bytes_per_second', None, f'sum(rate(node_network_receive_bytes_total{{{target},{_NODE_NIC}}}[{window}]))'),
            ('network_tx', 'bytes_per_second', None, f'sum(rate(node_network_transmit_bytes_total{{{target},{_NODE_NIC}}}[{window}]))'),
        )
    return (
        ('cpu', 'ratio', None, f'1 - avg(rate(windows_cpu_time_seconds_total{{{target},mode="idle"}}[{window}]))'),
        ('memory', 'ratio', None, f'1 - sum(windows_memory_available_bytes{{{target}}}) / sum(windows_memory_physical_total_bytes{{{target}}})'),
        ('disk', 'ratio', 'volume',
         f'1 - sum by (volume) (windows_logical_disk_free_bytes{{{target},{_WINDOWS_DISK}}})'
         f' / sum by (volume) (windows_logical_disk_size_bytes{{{target},{_WINDOWS_DISK}}})'),
        ('network_rx', 'bytes_per_second', None, f'sum(rate(windows_net_bytes_received_total{{{target},{_WINDOWS_NIC}}}[{window}]))'),
        ('network_tx', 'bytes_per_second', None, f'sum(rate(windows_net_bytes_sent_total{{{target},{_WINDOWS_NIC}}}[{window}]))'),
    )


def _matrix(raw):
    def finite(value):
        number = float(value)
        _need(math.isfinite(number))
        return number
    value = json.loads(raw, parse_float=finite, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    _need(type(value) is dict and value.get('status') == 'success' and type(value.get('data')) is dict)
    _need(value['data'].get('resultType') == 'matrix')
    result = value['data'].get('result')
    _need(type(result) is list and len(result) <= MAX_RESULT_SERIES)
    series = []
    for item in result:
        _need(type(item) is dict and type(item.get('metric')) is dict and type(item.get('values')) is list)
        samples = {}
        for sample in item['values']:
            _need(type(sample) is list and len(sample) == 2 and type(sample[0]) in (int, float))
            samples[int(sample[0])] = _number(sample[1])
        series.append((item['metric'], samples))
    return series


def _clean(value, unit):
    if value is None:
        return None
    if unit == 'ratio':
        return min(max(value, 0.0), 1.0) if -0.05 <= value <= 1.05 else None
    return value if 0 <= value < 2 ** 53 else None


def _points(samples, unit, start, end, step):
    return [[moment, _clean(samples.get(moment), unit)] for moment in range(start, end + 1, step)]


def _envelope(reader, host_key, span, status, series=()):
    return {'schema_version': SCHEMA_VERSION, 'generated_at': _iso(reader._clock()), 'host_key': host_key,
            'range': span, 'step_seconds': RANGES[span][1], 'source': {'status': status}, 'series': list(series)}


async def read_history(reader, actor, host_key, span, targets_parser):
    if type(host_key) is not str or not _HOST_KEY.fullmatch(host_key) or span not in RANGES:
        raise HubError('invalid_infrastructure_filter')
    reader._admit(actor)
    if reader.origin is None:
        return _envelope(reader, host_key, span, 'not_configured')
    seconds, step, window = RANGES[span]
    end = int(reader._clock().timestamp()) // step * step
    start = end - seconds
    budget = _Budget()
    targets = None
    results = {}
    planned = 0
    missing = False
    # More targets for one host than are charted: say so instead of reporting ok.
    truncated = False
    try:
        async with asyncio.timeout(DEADLINE_SECONDS):
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False, transport=reader._transport,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
                timeout=httpx.Timeout(5, connect=2, write=2, pool=2),
                headers={'Accept': 'application/json', 'Accept-Encoding': 'identity'},
            ) as client:
                try:
                    targets = targets_parser(await reader._get(client, TARGET_PATH, budget))
                except (httpx.HTTPError, ValueError, TypeError, RecursionError, UnicodeError):
                    pass
                if targets is not None:
                    inventory = reader._inventory(actor)
                    _need(type(inventory) is list)
                    rows = [row for row in normalize_targets(targets, inventory)['rows'] if row['host_key'] == host_key]
                    if not rows:
                        missing = True
                    truncated = len(rows) > MAX_TARGETS_PER_HOST
                    plan = [(index, kind, query)
                            for index, row in enumerate(rows[:MAX_TARGETS_PER_HOST])
                            for kind in ((row['exporter'],) if row['exporter'] in ('node', 'windows')
                                         else ('node', 'windows') if row['exporter'] is None else ())
                            for query in _series_queries(kind, row['job'], row['instance'], window)]
                    planned = len(plan)

                    async def fetch(index, kind, query):
                        key, unit, label, promql = query
                        path = '/api/v1/query_range?' + urlencode(
                            {'query': promql, 'start': start, 'end': end, 'step': step})
                        try:
                            results[(index, kind, key)] = (unit, label, _matrix(await reader._get(client, path, budget)))
                        except (httpx.HTTPError, ValueError, TypeError, RecursionError, UnicodeError):
                            pass

                    try:
                        async with asyncio.TaskGroup() as group:
                            for item in plan:
                                group.create_task(fetch(*item))
                    except* _Exhausted:
                        pass
    except TimeoutError:
        pass
    reader._admit(actor)
    if missing:
        raise HubError('infrastructure_machine_not_found', 404)
    if targets is None:
        return _envelope(reader, host_key, span, 'unreachable')
    series, seen = [], set()
    omitted_series = False
    for (_, _, key), (unit, label_name, found) in sorted(results.items()):
        rows = []
        for labels, samples in found:
            label = labels.get(label_name) if label_name else None
            if label_name and not _plain(label, 128):
                continue
            if any(value is not None for value in samples.values()):
                if (key, label) in seen or any(existing == label for existing, _ in rows):
                    omitted_series = True
                else:
                    rows.append((label, samples))
        if len(rows) > MAX_DISKS:
            omitted_series = True
        for label, samples in sorted(rows, key=lambda row: row[0] or '')[:MAX_DISKS]:
            seen.add((key, label))
            series.append({'key': key, 'label': label, 'unit': unit, 'points': _points(samples, unit, start, end, step)})
    return _envelope(reader, host_key, span, 'ok' if len(results) == planned and not truncated and not omitted_series else 'partial', series)
