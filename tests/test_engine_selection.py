"""Offline disjoint-Engine selection record: refusals and what it does not claim."""
import copy
import json
from pathlib import PurePosixPath

import pytest

from engineering_hub import engine_selection as selection
from engineering_hub.engine_selection import SelectionError, parse_selection, validate_selection


SHA = 'a' * 64


def evidence(kinds=selection.EVIDENCE_KINDS):
    return [{'kind': kind, 'note_path': f'reviews/engine-{kind}.md', 'sha256': SHA} for kind in sorted(kinds)]


def record(**changes):
    value = {
        'schema': selection.SCHEMA,
        'enabled': False,
        'machine_id': 'staging-pc',
        'engine': {
            'engine_id': '0b9e1d5c-7d41-4f0e-9a51-3f1f0f6c2a11',
            'mode': 'rootless',
            'run_as_uid': 10002,
            'socket_path': '/run/user/10002/eh-pilot-b/engine.sock',
            'data_root': '/var/lib/eh-pilot-b/docker-data',
        },
        'budget': {'cpu_millis': 1000, 'memory_bytes': 3 << 30, 'disk_bytes': 3 << 30},
        'approval': None,
        'evidence': [],
    }
    for key, update in changes.items():
        if isinstance(update, dict) and isinstance(value.get(key), dict):
            value[key] = {**value[key], **update}
        else:
            value[key] = update
    return value


def refused(data, code):
    with pytest.raises(SelectionError) as caught:
        validate_selection(data)
    assert caught.value.code == code
    return caught.value


def test_default_record_is_disabled_and_claims_nothing():
    result = validate_selection(record())
    assert result.status == 'disabled'
    assert result.socket_path == PurePosixPath('/run/user/10002/eh-pilot-b/engine.sock')
    assert set(result.limits) >= {'lexical_paths_only', 'no_mount_or_symlink_proof', 'no_namespace_proof', 'no_activation'}


def test_enabled_needs_human_decision_and_every_evidence_kind():
    refused(record(enabled=True, evidence=evidence()), 'enabled_without_approval')
    approval = {'decision_id': '11111111-2222-4333-8444-555555555555', 'approved_by': 'human:cf:' + 'b' * 64}
    refused(record(enabled=True, approval=approval, evidence=evidence(selection.EVIDENCE_KINDS - {'capacity'})),
            'enabled_without_evidence')
    result = validate_selection(record(enabled=True, approval=approval, evidence=evidence()))
    # Enabling still activates nothing.
    assert result.status == 'selected_unverified'
    assert result.approved_by == approval['approved_by']


@pytest.mark.parametrize('actor', ['codex', 'claude', 'installation:11111111-2222-4333-8444-555555555555', 'human:0', 'human:cf:ABC'])
def test_only_a_human_can_approve(actor):
    refused(record(approval={'decision_id': '11111111-2222-4333-8444-555555555555', 'approved_by': actor}), 'approval_actor')


@pytest.mark.parametrize('engine_id', [selection.HELD_ENGINE_ID, selection.HELD_ENGINE_ID.upper()])
def test_refuses_the_held_engine(engine_id):
    refused(record(engine={'engine_id': engine_id}), 'held_engine')


def test_refuses_the_held_runtime_account():
    refused(record(engine={'run_as_uid': selection.HELD_RUNTIME_UID,
                           'socket_path': '/run/eh-pilot-b/engine.sock'}), 'held_runtime_account')


@pytest.mark.parametrize('socket', [
    '/var/lib/eh-pilot-volume-test-v1/daemon-home/run-recovery-c930430c-v1/engine.sock',
    '/var/lib/eh-pilot-volume-test-v1/new.sock',
    '/run/user/10001/eh-exec-f07e-v2/engine.sock',
    '/var/lib/eh-test-storage-recovery-20261024-_pb9sahn/engine.sock',
])
def test_refuses_sockets_in_protected_roots(socket):
    refused(record(engine={'socket_path': socket}), 'socket_in_protected_root')


@pytest.mark.parametrize('root', [
    '/var/lib/eh-pilot-volume-test-v1',
    '/var/lib/eh-pilot-volume-test-v1/daemon-home/docker-data',
    '/var/lib/eh-pilot-test-v1.ext4',
    '/var/lib',             # contains every protected root
    '/run/user',            # contains the held account's runtime
    '/var/lib/eh-test-storage-recovery-20261024-_pb9sahn/copy',
])
def test_refuses_data_roots_overlapping_protected_roots(root):
    refused(record(engine={'data_root': root}), 'data_root_overlaps_protected_root')


@pytest.mark.parametrize('socket', ['/var/run/docker.sock', '/run/docker.sock', '/run/containerd/containerd.sock'])
def test_refuses_the_system_engine_socket(socket):
    refused(record(engine={'socket_path': socket}), 'socket_is_system_engine')


@pytest.mark.parametrize('root', ['/var/lib/docker', '/var/lib/docker/volumes', '/var/lib/containerd'])
def test_refuses_the_system_engine_data_root(root):
    refused(record(engine={'data_root': root}), 'data_root_overlaps_system_engine')


@pytest.mark.parametrize('root', [
    '/var/lib/eh-pilot-volume-test-v10',       # shares a text prefix, not a path component
    '/var/lib/eh-pilot-volume-test-v1-b',
    '/var/lib/eh-pilot-test-v1.ext4.d',
    '/var/lib/docker-eh-pilot-b',
])
def test_near_miss_prefixes_are_distinct_paths(root):
    assert validate_selection(record(engine={'data_root': root})).data_root == PurePosixPath(root)


@pytest.mark.parametrize('path', [
    'relative/engine.sock', '/run/eh//engine.sock', '/run/eh/./engine.sock', '/run/eh/../user/10001/x.sock',
    '/run/eh/engine.sock/', '/', '/run/eh/\x00.sock', '/run/eh\\engine.sock', '', 42, None,
])
def test_paths_must_already_be_normal_absolute_spellings(path):
    refused(record(engine={'socket_path': path}), 'socket_path')


def test_dot_dot_cannot_reach_a_protected_root_through_a_harmless_prefix():
    refused(record(engine={'data_root': '/srv/eh/../../var/lib/eh-pilot-volume-test-v1'}), 'data_root')


def test_socket_may_not_live_inside_the_data_root():
    refused(record(engine={'socket_path': '/var/lib/eh-pilot-b/docker-data/engine.sock'}), 'socket_inside_data_root')


@pytest.mark.parametrize('mode,uid', [('rootless', 0), ('rootful', 10002)])
def test_mode_and_account_must_agree(mode, uid):
    refused(record(engine={'mode': mode, 'run_as_uid': uid, 'socket_path': '/run/eh-pilot-b/engine.sock'}), 'mode_uid_mismatch')


@pytest.mark.parametrize('field,value,code', [
    ('cpu_millis', 0, 'budget_cpu'), ('cpu_millis', True, 'budget_cpu'), ('cpu_millis', 1.5, 'budget_cpu'),
    ('cpu_millis', 64_001, 'budget_cpu'), ('memory_bytes', -1, 'budget_memory'), ('memory_bytes', '3G', 'budget_memory'),
    ('disk_bytes', (1 << 44) + 1, 'budget_disk'), ('disk_bytes', None, 'budget_disk'),
])
def test_budgets_are_explicit_positive_bounded_integers(field, value, code):
    refused(record(budget={field: value}), code)


def test_namespace_ids_are_not_accepted_as_isolation_evidence():
    data = record()
    data['engine']['user_namespace'] = 4026532459
    refused(data, 'engine_shape')


@pytest.mark.parametrize('item,code', [
    ({'kind': 'trust_me', 'note_path': 'reviews/x.md', 'sha256': SHA}, 'evidence_kind'),
    ({'kind': 'capacity', 'note_path': 'reviews/../secrets.md', 'sha256': SHA}, 'evidence_note_path'),
    ({'kind': 'capacity', 'note_path': '/etc/passwd.md', 'sha256': SHA}, 'evidence_note_path'),
    ({'kind': 'capacity', 'note_path': 'reviews/x.md', 'sha256': 'A' * 64}, 'evidence_sha256'),
    ({'kind': 'capacity', 'note_path': 'reviews/x.md', 'sha256': SHA, 'verified': True}, 'evidence_shape'),
])
def test_evidence_references_are_closed(item, code):
    refused(record(evidence=[item]), code)


def test_each_evidence_kind_is_cited_once():
    refused(record(evidence=evidence({'capacity'}) * 2), 'evidence_duplicate_kind')


def test_unknown_or_missing_fields_are_refused():
    data = record()
    data['activate'] = True
    refused(data, 'record_shape')
    data = record()
    del data['budget']
    refused(data, 'record_shape')
    refused(record(schema='engineering-hub.engine-selection.v2'), 'schema')
    refused(record(machine_id='production'), 'machine_id')
    refused(record(enabled='true'), 'enabled')


def test_json_parsing_refuses_duplicates_and_oversized_records():
    text = json.dumps(record())
    assert parse_selection(text).status == 'disabled'
    duplicate = text.replace('"enabled": false', '"enabled": false, "enabled": true')
    with pytest.raises(SelectionError) as caught:
        parse_selection(duplicate)
    assert caught.value.code == 'duplicate_key'
    with pytest.raises(SelectionError) as caught:
        parse_selection(' ' * (selection.MAX_RECORD_BYTES + 1))
    assert caught.value.code == 'record_size'
    with pytest.raises(SelectionError) as caught:
        parse_selection('{')
    assert caught.value.code == 'record_json'


def test_refusals_never_echo_input_values():
    secret_path = '/var/lib/eh-pilot-volume-test-v1/private-name-1234'
    error = refused(record(engine={'data_root': secret_path}), 'data_root_overlaps_protected_root')
    assert secret_path not in str(error) and 'private-name-1234' not in repr(error)


def test_validation_does_not_mutate_its_input():
    data = record(enabled=True, approval={'decision_id': '11111111-2222-4333-8444-555555555555',
                                          'approved_by': 'human:42'}, evidence=evidence())
    before = copy.deepcopy(data)
    validate_selection(data)
    assert data == before


def test_module_has_no_host_or_network_access():
    with open(selection.__file__, encoding='utf-8') as handle:
        text = handle.read()
    for forbidden in ('import os', 'import socket', 'import subprocess', 'urllib', 'httpx', 'open('):
        assert forbidden not in text, forbidden
