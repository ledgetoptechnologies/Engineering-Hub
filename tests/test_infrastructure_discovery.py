"""Pure discovery contract tests; no Prometheus transport or registry writes."""

from engineering_hub.infrastructure_discovery import normalize_targets


def target(job='node', instance='box:9100', **labels):
    return {'labels': {'job': job, 'instance': instance, **labels}}


def test_duplicate_pairs_are_all_dropped_and_malformed_rows_warn():
    result = normalize_targets([target(), target(host='other'), None, {'labels': []},
                                target(instance='bad\ninstance')], [])
    assert result == {'rows': [], 'warnings': ['invalid_target'], 'omitted_count': 0}


def test_exact_host_grouping_and_inventory_association():
    inventory = [{'device_id': 'desk', 'hostnames': ['desk']}]
    result = normalize_targets([target(host='desk'), target('nvidia', 'box:9400', host='desk'),
                                target('windows', 'desk:9182')], inventory)
    rows = result['rows']
    assert rows[0]['host_key'] == rows[1]['host_key']
    assert rows[0]['host_key'].startswith('host:')
    assert rows[0]['device_id'] == rows[1]['device_id'] == 'desk'
    assert rows[0]['association'] == rows[1]['association'] == 'matched'
    assert rows[2]['association'] == 'unregistered'
    assert rows[2]['host_key'].startswith('target:')
    assert result['warnings'] == []


def test_ambiguous_alias_and_unknown_exporter_preserved():
    result = normalize_targets([target('other', host='shared', role='worker', env='bad\x7f')], [
        {'device_id': 'one', 'hostnames': ['shared']},
        {'device_id': 'two', 'hostnames': ['shared']},
    ])
    row = result['rows'][0]
    assert row['association'] == 'ambiguous' and row['device_id'] is None
    assert row['exporter'] is None and row['probe_id'] is None
    assert row['labels'] == {'host': 'shared', 'role': 'worker'}
    assert result['warnings'] == ['ambiguous_inventory', 'unknown_exporter']


def test_existing_gpu_job_name_is_recognized():
    row = normalize_targets([target('nvidia-gpu', 'gpu:9400')], [])['rows'][0]
    assert row['exporter'] == 'nvidia'


def test_host_and_target_hash_namespaces_and_invalid_host():
    result = normalize_targets([target(host='bad\n'), target('windows', 'box:9182', host='box')], [])
    assert result['rows'][0]['host_label'] is None
    assert result['rows'][0]['host_key'].startswith('target:')
    assert result['rows'][1]['host_key'].startswith('host:')


def test_order_independent_truncation_and_omitted_count():
    targets = [target(instance=f'box-{i:03}:9100') for i in range(205)]
    forward = normalize_targets(targets, [])
    assert forward == normalize_targets(list(reversed(targets)), [])
    assert len(forward['rows']) == 200 and forward['omitted_count'] == 5
    assert forward['warnings'] == ['targets_truncated']


def test_invalid_inventory_never_creates_association():
    inventory = [{'device_id': 'Bad ID', 'hostnames': ['box']},
                 {'device_id': 'valid', 'hostnames': 'box'}, None]
    assert normalize_targets([target(host='box')], inventory)['rows'][0]['association'] == 'unregistered'


def test_decoded_input_caps_refuse_oversize_before_normalization():
    for targets, inventory, message in [
        ([target()] * 4097, [], 'discovery_target_limit'),
        ([], [{}] * 4097, 'discovery_inventory_limit'),
    ]:
        try:
            normalize_targets(targets, inventory)
        except ValueError as error:
            assert str(error) == message
        else:
            raise AssertionError('oversize decoded input accepted')


def test_third_duplicate_cannot_restore_an_ambiguous_pair():
    result = normalize_targets([target(host='one'), target(host='two'), target(host='one')], [])
    assert result == {'rows': [], 'warnings': ['invalid_target'], 'omitted_count': 0}
