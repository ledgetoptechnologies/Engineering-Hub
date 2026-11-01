"""Shared pure launch-profile parser: legacy bytes, v3 policy and exact-bytes verification."""
import hashlib
import json
from pathlib import Path

import pytest

from engineering_hub import executor_launch_profile as core


def profile(root='/operator/hub-pilot-synthetic', **changes):
    mounts = []
    for role, target in core.LAUNCH_TARGETS.items():
        source = ('/run/test-docker.sock' if role == 'engine_socket' else
                  '/run/test-issuer/issuer.sock' if role == 'issuer_socket' else root + '/' + role)
        kind = 'socket' if role.endswith('_socket') else 'file' if role == 'config' else 'directory'
        mounts.append(dict(role=role, source=source, target=source if role == 'generations' else target,
            kind=kind, read_only=role not in {'state', 'generations'},
            uid=0 if role == 'issuer_socket' else 110001, gid=110001,
            mode=0o660 if kind == 'socket' else 0o600 if kind == 'file' else 0o700,
            content_digest='sha256:' + 'f' * 64 if kind == 'file' else None))
    return dict(user='10001:10001', supplementary_groups=[],
        entrypoint=['/opt/venv/bin/python', '-I', '-m', 'hub_container_executor.installed_runner'],
        command=['--config', '/operator/runner.json'], environment=['PATH=/usr/bin:/bin'],
        fixture_root=root, memory_bytes=128 * 1024**2, cpu_millicores=250, pids_limit=32,
        tmpfs_bytes=8 * 1024**2, mounts=mounts,
        runner_file='/opt/venv/lib/python3.12/site-packages/hub_container_executor/installed_runner.py',
        runner_digest='sha256:' + 'e' * 64, **changes)


def policy(**changes):
    return {'kind': 'disjoint-v1', 'engine_id': 'test-daemon', 'daemon_uid': 10002, 'daemon_gid': 10002,
            'uid_map': [[0, 10002, 1], [1, 100001, 65536]], 'gid_map': [[0, 10002, 1], [1, 100001, 65536]],
            'held_ranges_digest': 'sha256:' + 'a' * 64, **changes}


def gate_profile(root='/operator/hub-pilot-synthetic'):
    data = profile(root, daemon_policy=policy(), gate_channel='protected-socket-v1')
    source = root + '/gate-control'
    data['mounts'].append(dict(role='gate_channel_directory', source=source, target=source,
        kind='directory', read_only=True, uid=0, gid=0, mode=0o711, content_digest=None))
    return data


def test_gate_profile_is_explicit_and_preserves_legacy_digest():
    legacy = profile()
    assert core.ExecutorLaunchProfile.model_validate(legacy).digest() == digest_of(core.canonical(legacy))
    gate = gate_profile()
    parsed = core.ExecutorLaunchProfile.model_validate(gate)
    assert parsed.gate_channel == 'protected-socket-v1'
    assert parsed.digest() == digest_of(core.canonical(gate))
    assert parsed.digest() != core.ExecutorLaunchProfile.model_validate(legacy).digest()


def test_pair_ledger_profile_is_explicit_and_excludes_gate_authority():
    old = profile(); old_bytes = core.canonical(old)
    data = gate_profile(); del data['gate_channel']
    data['gate_ledger_channel'] = 'pair-outcome-listener-v1'
    parsed = core.ExecutorLaunchProfile.model_validate(data)
    assert parsed.gate_channel is None and parsed.gate_completion is None
    assert parsed.gate_ledger_channel == 'pair-outcome-listener-v1'
    assert core.canonical(core.ExecutorLaunchProfile.model_validate(old).enrollment_value()) == old_bytes
    for extra in ({'gate_channel': 'protected-socket-v1'}, {'generation_isolation': {}}, {'gate_completion': {}}):
        with pytest.raises(Exception): core.ExecutorLaunchProfile.model_validate(dict(data, **extra))


def test_gate_completion_policy_is_explicit_and_bound_to_profile():
    data = gate_profile()
    before = core.ExecutorLaunchProfile.model_validate(data).digest()
    data['gate_completion'] = {'candidate_executable_digest': 'sha256:' + 'a' * 64,
        'rollback_executable_digest': 'sha256:' + 'b' * 64,
        'health_contract': 'engineering-hub.health-json-build-commit.v1'}
    parsed = core.ExecutorLaunchProfile.model_validate(data)
    assert parsed.digest() != before
    assert parsed.gate_completion.candidate_executable_digest == 'sha256:' + 'a' * 64
    data['gate_completion']['candidate_executable_digest'] = 'sha256:' + 'A' * 64
    with pytest.raises(Exception): core.ExecutorLaunchProfile.model_validate(data)


def test_generation_isolation_is_signed_and_rejects_overlapping_accounts():
    data = gate_profile()
    data['gate_completion'] = {'candidate_executable_digest': 'sha256:' + 'a' * 64,
        'rollback_executable_digest': 'sha256:' + 'b' * 64,
        'health_contract': 'engineering-hub.health-json-build-commit.v1'}
    data['gate_ledger_channel'] = 'supervisor-listener-v1'
    old = core.ExecutorLaunchProfile.model_validate(data).digest()
    data['generation_isolation'] = dict(kind='separate-provider-v1', app_uid=10002,
        app_gid=10002, provider_host_uid=200000, provider_host_gid=200000,
        provider_public_key='1' * 64)
    parsed = core.ExecutorLaunchProfile.model_validate(data)
    assert parsed.digest() != old
    assert parsed.digest() == digest_of(core.canonical(data))
    for key, value in [('app_uid', 10001), ('app_gid', 10001),
                       ('provider_host_uid', 110001), ('provider_host_gid', 110001),
                       ('provider_public_key', '0' * 64)]:
        invalid = dict(data, generation_isolation=dict(data['generation_isolation'], **{key: value}))
        with pytest.raises(Exception): core.ExecutorLaunchProfile.model_validate(invalid)
    with pytest.raises(Exception):
        core.ExecutorLaunchProfile.model_validate(dict(data, generation_isolation=None))


def test_custodian_control_has_closed_signed_host_pins_and_preserves_prior_profiles():
    data = gate_profile()
    data['gate_completion'] = {'candidate_executable_digest': 'sha256:' + 'a' * 64,
        'rollback_executable_digest': 'sha256:' + 'b' * 64,
        'health_contract': 'engineering-hub.health-json-build-commit.v1'}
    data['gate_ledger_channel'] = 'supervisor-listener-v1'
    data['generation_isolation'] = dict(kind='separate-provider-v1', app_uid=10002,
        app_gid=10002, provider_host_uid=200000, provider_host_gid=200000, provider_public_key='1' * 64)
    old = core.ExecutorLaunchProfile.model_validate(data)
    assert old.enrollment_value() == data and old.digest() == digest_of(core.canonical(data))
    data['custodian_control'] = {'kind': 'protected-listener-v1', 'listener_path': '/run/custodian/issuer.sock',
        'journal_path': '/root/custodian/journal.sqlite', 'journal_enrollment_path': '/root/custodian/enrollment.json',
        'journal_enrollment_digest': 'sha256:' + 'a' * 64, 'copy_policy_path': '/root/custodian/copy.json',
        'copy_policy_digest': 'sha256:' + 'b' * 64, 'owner_policy_path': '/root/custodian/owner.json',
        'owner_policy_digest': 'sha256:' + 'c' * 64}
    parsed = core.ExecutorLaunchProfile.model_validate(data)
    assert parsed.digest() == digest_of(core.canonical(data)) and parsed.digest() != old.digest()
    for key, value in [('listener_path', '/run/custodian/../issuer.sock'),
                       ('listener_path', '/run/' + 'a' * 110),
                       ('journal_path', '/root/custodian/copy.json'),
                       ('copy_policy_path', '/operator/hub-pilot-synthetic/state/policy.json'),
                       ('owner_policy_digest', 'sha256:' + 'A' * 64),
                       ('unknown', 'untrusted')]:
        bad = dict(data, custodian_control=dict(data['custodian_control'], **{key: value}))
        with pytest.raises(Exception): core.ExecutorLaunchProfile.model_validate(bad)
    for bad in (dict(data, custodian_control=None), {k: v for k, v in data.items() if k != 'generation_isolation'}):
        with pytest.raises(Exception): core.ExecutorLaunchProfile.model_validate(bad)


@pytest.mark.parametrize('change', ['missing_mode', 'writable', 'wrong_owner', 'alias', 'missing_policy', 'extra_mount'])
def test_gate_profile_denies_unbound_mount(change):
    data = gate_profile(); mount = data['mounts'][-1]
    if change == 'missing_mode': data.pop('gate_channel')
    if change == 'writable': mount['read_only'] = False
    if change == 'wrong_owner': mount['uid'] = 10001
    if change == 'alias': mount['target'] = '/run/hub-gate'
    if change == 'missing_policy': data.pop('daemon_policy')
    if change == 'extra_mount': data['mounts'].append(dict(mount))
    with pytest.raises(Exception): core.ExecutorLaunchProfile.model_validate(data)


def digest_of(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def test_legacy_digest_is_the_unchanged_canonical_bytes():
    raw = core.canonical(profile())
    parsed = core.ExecutorLaunchProfile.model_validate(profile())
    assert parsed.digest() == digest_of(raw)
    assert core.canonical(parsed.enrollment_value()) == raw


def test_verify_accepts_exact_legacy_and_v3_bytes():
    legacy = core.canonical(profile())
    assert core.verify_profile_bytes(legacy, digest_of(legacy)).daemon_policy is None
    v3 = core.canonical(profile(daemon_policy=policy()))
    parsed = core.verify_profile_bytes(v3, digest_of(v3))
    assert parsed.daemon_policy.uid_map == ((0, 10002, 1), (1, 100001, 65536))
    assert parsed.daemon_policy.held_ranges_digest == 'sha256:' + 'a' * 64


def test_verify_refuses_a_different_pinned_digest():
    v3 = core.canonical(profile(daemon_policy=policy()))
    other = core.canonical(profile(daemon_policy=policy(held_ranges_digest='sha256:' + 'b' * 64)))
    with pytest.raises(core.LaunchProfileError):
        core.verify_profile_bytes(v3, digest_of(other))


@pytest.mark.parametrize('mutate', [
    lambda raw: raw + b' ',                                            # trailing byte
    lambda raw: json.dumps(json.loads(raw), indent=1).encode(),        # non-canonical layout
    lambda raw: raw.replace(b'{', b'{"cpu_millicores":250,', 1),       # duplicate key
    lambda raw: raw.replace(b'"cpu_millicores":250', b'"cpu_millicores":250.0'),  # float
    lambda raw: raw.replace(b'"cpu_millicores":250', b'"cpu_millicores":NaN'),
    lambda raw: b'\xff' + raw,
])
def test_verify_refuses_bytes_that_are_not_exactly_canonical(mutate):
    raw = mutate(core.canonical(profile()))
    with pytest.raises(core.LaunchProfileError):
        core.verify_profile_bytes(raw, digest_of(raw))


def test_verify_refuses_canonical_bytes_with_an_explicit_null_policy():
    # Canonical form, so only the legacy no-null rule can refuse it.
    raw = core.canonical({**profile(), 'daemon_policy': None})
    with pytest.raises(core.LaunchProfileError):
        core.verify_profile_bytes(raw, digest_of(raw))


@pytest.mark.parametrize('raw,expected', [
    ('{}', 'sha256:' + 'a' * 64), (b'', 'sha256:' + 'a' * 64), (b'{}', 'a' * 64),
    (b'{}', 'sha256:' + 'A' * 64), (b'{}', None), (b' ' * (core.MAX_PROFILE_BYTES + 1), 'sha256:' + 'a' * 64),
])
def test_verify_refuses_malformed_arguments(raw, expected):
    with pytest.raises(core.LaunchProfileError):
        core.verify_profile_bytes(raw, expected)


def test_refusals_never_echo_values():
    raw = core.canonical(profile(root='/operator/hub-pilot-SECRETNAME'))
    with pytest.raises(core.LaunchProfileError) as caught:
        core.verify_profile_bytes(raw, digest_of(raw))
    assert 'SECRETNAME' not in str(caught.value) and str(caught.value) == 'executor launch profile invalid'


def test_core_module_has_no_host_or_executor_access():
    text = Path(core.__file__).read_text(encoding='utf-8')
    for forbidden in ('import fcntl', 'import socket', 'import subprocess', 'import sqlite3',
                      'hub_container_executor import', 'from hub_container_executor', 'open(',
                      # Host path semantics would reject POSIX profile paths on Windows.
                      'os.path', 'import os\n', 'from pathlib import Path\n'):
        assert forbidden not in text, forbidden


def test_posix_paths_validate_the_same_under_windows_path_rules(monkeypatch):
    import ntpath
    import os
    raw = core.canonical(profile(daemon_policy=policy()))
    # If the parser consulted the host's path module, Windows rules would rewrite '/' paths and refuse them.
    monkeypatch.setattr(os, 'path', ntpath)
    assert core.verify_profile_bytes(raw, digest_of(raw)).fixture_root == '/operator/hub-pilot-synthetic'


def test_validation_errors_are_sanitized():
    raw = core.canonical({**profile(), 'memory_bytes': 'SECRETVALUE'})
    with pytest.raises(core.LaunchProfileError) as caught:
        core.verify_profile_bytes(raw, digest_of(raw))
    assert type(caught.value) is core.LaunchProfileError
    assert 'SECRETVALUE' not in str(caught.value) and caught.value.__cause__ is None


def test_lone_surrogate_escape_fails_closed_without_leaking():
    raw = core.canonical(profile()).replace(b'"PATH=/usr/bin:/bin"', b'"PATH=\\ud800SECRET"')
    assert b'\\ud800' in raw
    with pytest.raises(core.LaunchProfileError) as caught:
        core.verify_profile_bytes(raw, digest_of(raw))
    assert 'SECRET' not in str(caught.value)


@pytest.mark.parametrize('text', ['0 10002 1\n1 ' + '9' * 5000 + ' 65536\n', '0 10002 1\n1 10000000000 65536\n'])
def test_oversized_map_numbers_refuse_closed(text):
    with pytest.raises(core.LaunchProfileError):
        core._proc_map_rows(text)


def test_launch_targets_are_read_only():
    with pytest.raises(TypeError):
        core.LAUNCH_TARGETS['config'] = '/tmp/elsewhere'
    with pytest.raises(TypeError):
        del core.LAUNCH_TARGETS['config']
    assert core.LAUNCH_TARGETS['config'] == '/operator/runner.json'


def broker_profile():
    value = profile(daemon_policy=policy(), broker_mode='broker-v1')
    value['mounts'] = [item for item in value['mounts']
                       if item['role'] not in {'engine_socket', 'generations'}]
    value['mounts'].append(dict(role='broker_socket', source='/run/test-broker/broker.sock',
        target='/run/engineering-hub/broker.sock', kind='socket', read_only=True,
        uid=0, gid=110001, mode=0o660, content_digest=None))
    return value


def test_broker_profile_is_signed_and_excludes_direct_authority():
    data = broker_profile()
    raw = core.canonical(data)
    parsed = core.verify_profile_bytes(raw, digest_of(raw))
    assert parsed.broker_mode == 'broker-v1'
    assert {item.role for item in parsed.mounts} == (set(core.LAUNCH_TARGETS)
        - {'engine_socket', 'generations'}) | {'broker_socket'}
    assert core.canonical(parsed.enrollment_value()) == raw
    assert parsed.digest() != core.ExecutorLaunchProfile.model_validate(profile(daemon_policy=policy())).digest()


@pytest.mark.parametrize('change', ['engine', 'generation', 'missing_broker', 'no_policy', 'null', 'wrong_kind'])
def test_broker_profile_rejects_direct_or_ambiguous_authority(change):
    data = broker_profile()
    if change in {'engine', 'generation'}:
        role = 'engine_socket' if change == 'engine' else 'generations'
        data['mounts'].append(next(item for item in profile()['mounts'] if item['role'] == role))
    elif change == 'missing_broker':
        data['mounts'] = [item for item in data['mounts'] if item['role'] != 'broker_socket']
    elif change == 'no_policy':
        del data['daemon_policy']
    elif change == 'null':
        data['broker_mode'] = None
    else:
        data['broker_mode'] = 'broker-v2'
    with pytest.raises((ValueError, RuntimeError)):
        core.ExecutorLaunchProfile.model_validate(data)


def coordinator_profile(**changes):
    mounts = []
    for role, target in core.COORDINATOR_TARGETS.items():
        socket_role = role == 'issuer_socket'
        kind = 'socket' if socket_role else 'file' if role == 'config' else 'directory'
        mounts.append(dict(role=role, source='/run/test-issuer/issuer.sock' if socket_role
                           else '/operator/hub-pilot-coordinator/' + role,
            target=target, kind=kind, read_only=role != 'state',
            uid=0 if socket_role else 110002, gid=110002,
            mode=0o660 if kind == 'socket' else 0o600 if kind == 'file' else 0o700,
            content_digest='sha256:' + 'f' * 64 if kind == 'file' else None))
    value = profile(root='/operator/hub-pilot-coordinator', daemon_policy=policy(),
                    role='coordinator', command_channel='inherited-seqpacket-v1')
    value.update(user='10002:10002', mounts=mounts,
        entrypoint=['/opt/venv/bin/python', '-I', '-m', 'hub_container_executor.installed_coordinator'],
        command=['serve', '--config', '/operator/coordinator.json'],
        runner_file='/opt/venv/lib/python3.12/site-packages/hub_container_executor/installed_coordinator.py')
    value.update(changes)
    return value


def trusted_local_profile():
    root = '/operator/hub-pilot-local'
    data = profile(root=root, daemon_policy=policy(), role='trusted-local-coordinator-v1')
    data.update(entrypoint=['/opt/venv/bin/python', '-I', '-m', 'hub_container_executor.trusted_local_session'],
        command=['--config', '/operator/coordinator.json', '--enrollment', '/operator-evidence/session.json'],
        runner_file='/opt/venv/lib/python3.12/site-packages/hub_container_executor/trusted_local_session.py')
    data['mounts'] = []
    for role, target in core.TRUSTED_LOCAL_COORDINATOR_TARGETS.items():
        kind = 'file' if role in {'local_config', 'local_enrollment'} else 'directory'
        data['mounts'].append(dict(role=role, source=root+'/'+role, target=target, kind=kind,
            read_only=role not in {'local_state', 'local_browser_directory'}, uid=110001, gid=110001,
            mode=0o600 if kind in {'file', 'socket'} else 0o700,
            content_digest='sha256:'+'a'*64 if kind=='file' else None))
    return data


def supervised_local_profile():
    data=trusted_local_profile(); data['role']='supervised-local-coordinator-v2'; data['command']=['--supervised']
    data['mounts'] += [
        dict(role='local_admission',source='/operator/hub-pilot-local/admission',
             target='/operator-admission/admission.json',kind='file',read_only=True,
             uid=110001,gid=110001,mode=0o600,content_digest='sha256:'+'b'*64),
        dict(role='local_issuer_socket',source='/run/owned-coordinator-issuer.sock',
             target='/run/engineering-hub/coordinator-issuer.sock',kind='socket',read_only=True,
             uid=0,gid=110001,mode=0o660,content_digest=None)]
    return data


def supervised_local_oauth_profile():
    data=supervised_local_profile();data['role']='supervised-local-coordinator-v3'
    data['mounts'].append(dict(role='local_oauth_socket',source='/run/owned-github-oauth.sock',
        target='/run/engineering-hub/github-oauth.sock',kind='socket',read_only=True,
        uid=120001,gid=110001,mode=0o660,content_digest=None))
    return data


def test_supervised_local_profile_is_new_closed_shape_and_legacy_unchanged():
    legacy=trusted_local_profile(); before=core.canonical(legacy)
    data=supervised_local_profile()
    signed=core.ExecutorLaunchProfile.model_validate(data)
    assert signed.enrollment_value()==data
    assert core.verify_profile_bytes(core.canonical(data),signed.digest()).enrollment_value()==data
    assert core.canonical(core.ExecutorLaunchProfile.model_validate(legacy).enrollment_value())==before
    for role,changes in [('local_issuer_socket',{'uid':110001}),('local_issuer_socket',{'mode':0o600}),
                         ('local_admission',{'read_only':False}),('local_admission',{'content_digest':None})]:
        bad=dict(data);bad['mounts']=[dict(item) for item in data['mounts']]
        _swap_mount(bad,role,**changes)
        with pytest.raises((ValueError,RuntimeError)): core.ExecutorLaunchProfile.model_validate(bad)
    for changes in ({'command':['--config','/operator/coordinator.json']},{'supplementary_groups':[1]},
                    {'gate_channel':'protected-socket-v1'}):
        with pytest.raises((ValueError,RuntimeError)): core.ExecutorLaunchProfile.model_validate(dict(data,**changes))


def test_supervised_commitment_requires_independent_raw_and_complete_release_pins():
    pytest.importorskip('fcntl')
    from hub_container_executor.configuration_commitment import supervised_local_coordinator_commitment, trusted_local_coordinator_commitment
    from hub_container_executor.coordinator_admission import admission_commitment, parse_admission
    from tests.test_hub_coordinator_admission import document
    from tests.test_hub_container_executor_installed_coordinator import github_configuration, canonical
    from tests.test_hub_trusted_queued_session import v2_document, encode
    from tests.test_hub_unified_pilot_startup import profile_document
    from engineering_hub.trusted_queued_session import SCHEMA_V3
    config=github_configuration();config['manifest']=profile_document()['manifest']
    config.update(database_path='/var/lib/engineering-hub/local-coordinator/hub.sqlite',
        coordinator_socket_path='/run/hub-browser/hub.sock',executor_socket_path='/run/hub-executor/executor.sock',
        secret_root='/var/lib/engineering-hub/local-secrets')
    enrollment=v2_document();enrollment.update(schema=SCHEMA_V3,operator_actor='human:123')
    config_raw,enrollment_raw=canonical(config),encode(enrollment)
    data=trusted_local_profile();data['role']='supervised-local-coordinator-v2';data['command']=['--supervised']
    data['daemon_policy']['engine_id']=enrollment['launch']['engine_id']
    _swap_mount(data,'local_config',content_digest=digest_of(config_raw))
    _swap_mount(data,'local_enrollment',content_digest=digest_of(enrollment_raw))
    adm=document(config_raw,enrollment_raw);normalized,_=admission_commitment(canonical(adm))
    data['mounts'] += [dict(role='local_admission',source='/operator/hub-pilot-local/admission',target='/operator-admission/admission.json',
        kind='file',read_only=True,uid=110001,gid=110001,mode=0o600,content_digest=normalized),
        dict(role='local_issuer_socket',source='/run/owned-coordinator-issuer.sock',target='/run/engineering-hub/coordinator-issuer.sock',
        kind='socket',read_only=True,uid=0,gid=110001,mode=0o660,content_digest=None)]
    profile=core.ExecutorLaunchProfile.model_validate(data)
    adm['release']['launch_profile_digest']=profile.digest(); raw=canonical(adm)
    commitment,raw_pin=admission_commitment(raw); assert commitment==normalized
    release=parse_admission(raw).release
    supervised_local_coordinator_commitment(profile,config_raw,enrollment_raw,raw,root_raw_digest=raw_pin,independent_release=release)
    with pytest.raises(ValueError): trusted_local_coordinator_commitment(profile,config_raw,enrollment_raw)
    with pytest.raises(ValueError): supervised_local_coordinator_commitment(profile,config_raw,enrollment_raw,raw,root_raw_digest='sha256:'+'0'*64,independent_release=release)
    from dataclasses import replace
    with pytest.raises(ValueError): supervised_local_coordinator_commitment(profile,config_raw,enrollment_raw,raw,root_raw_digest=raw_pin,independent_release=replace(release,issuer_id='other'))
    changed=dict(adm);changed['release']=dict(adm['release'],launch_profile_digest='sha256:'+'f'*64);changed_raw=canonical(changed)
    assert admission_commitment(changed_raw)[0]==normalized
    with pytest.raises(ValueError): supervised_local_coordinator_commitment(profile,config_raw,enrollment_raw,changed_raw,root_raw_digest=admission_commitment(changed_raw)[1],independent_release=release)


def test_supervised_oauth_profile_binds_independent_broker_to_github_config():
    pytest.importorskip('fcntl')
    from hub_container_executor.configuration_commitment import supervised_local_coordinator_commitment
    from hub_container_executor.coordinator_admission import admission_commitment,parse_admission,SCHEMA_V2,OAUTH_SOCKET
    from tests.test_hub_coordinator_admission import document
    from tests.test_hub_container_executor_installed_coordinator import github_configuration,canonical
    from tests.test_hub_trusted_queued_session import v2_document,encode
    from tests.test_hub_unified_pilot_startup import profile_document
    from engineering_hub.trusted_queued_session import SCHEMA_V3
    config=github_configuration();config['manifest']=profile_document()['manifest']
    config.update(database_path='/var/lib/engineering-hub/local-coordinator/hub.sqlite',coordinator_socket_path='/run/hub-browser/hub.sock',
                  executor_socket_path='/run/hub-executor/executor.sock',secret_root='/var/lib/engineering-hub/local-secrets')
    enrollment=v2_document();enrollment.update(schema=SCHEMA_V3,operator_actor='human:123')
    cfg,session=canonical(config),encode(enrollment)
    def bind(client_id='fixture-client'):
        value=document(cfg,session);value['schema']=SCHEMA_V2
        value['oauth_broker']={'socket_path':OAUTH_SOCKET,'issuer_uid':77,'issuer_gid':88,'client_id':client_id,
                               'origin':'https://pilot.example.test','secret_digest':'sha256:'+'5'*64}
        normalized,_=admission_commitment(canonical(value))
        data=supervised_local_profile();data['role']='supervised-local-coordinator-v3';data['daemon_policy']['engine_id']=enrollment['launch']['engine_id']
        _swap_mount(data,'local_config',content_digest=digest_of(cfg));_swap_mount(data,'local_enrollment',content_digest=digest_of(session));_swap_mount(data,'local_admission',content_digest=normalized)
        data['mounts'].append(dict(role='local_oauth_socket',source='/run/owned-github-oauth.sock',target=OAUTH_SOCKET,
                                  kind='socket',read_only=True,uid=120001,gid=110001,mode=0o660,content_digest=None))
        profile=core.ExecutorLaunchProfile.model_validate(data);value['release']['launch_profile_digest']=profile.digest()
        raw=canonical(value);return profile,raw,parse_admission(raw).release,admission_commitment(raw)[1]
    profile,raw,release,pin=bind()
    supervised_local_coordinator_commitment(profile,cfg,session,raw,root_raw_digest=pin,independent_release=release)
    profile,raw,release,pin=bind('other-client')
    with pytest.raises(ValueError):supervised_local_coordinator_commitment(profile,cfg,session,raw,root_raw_digest=pin,independent_release=release)


def test_trusted_local_profile_is_distinct_closed_and_preserves_old_bytes():
    previous = coordinator_profile()
    assert core.ExecutorLaunchProfile.model_validate(previous).enrollment_value() == previous
    data = trusted_local_profile()
    raw = core.canonical(data)
    assert core.verify_profile_bytes(raw, digest_of(raw)).enrollment_value() == data
    for key, value in [('user', '10002:10002'), ('command', ['--config', '/operator/coordinator.json']),
                       ('command_channel', 'inherited-seqpacket-v1'), ('supplementary_groups', [123])]:
        with pytest.raises((ValueError, RuntimeError)):
            core.ExecutorLaunchProfile.model_validate(dict(data, **{key: value}))
    for role, changes in [('local_enrollment', {'read_only': False}),
                          ('local_executor_directory', {'uid': 10001}),
                          ('local_browser_directory', {'read_only': True})]:
        altered = trusted_local_profile()
        _swap_mount(altered, role, **changes)
        with pytest.raises((ValueError, RuntimeError)):
            core.ExecutorLaunchProfile.model_validate(altered)


def test_trusted_local_commitment_binds_paths_and_v3_operator():
    pytest.importorskip('fcntl')
    from hub_container_executor.configuration_commitment import trusted_local_coordinator_commitment
    from tests.test_hub_container_executor_installed_coordinator import github_configuration, canonical
    from tests.test_hub_trusted_queued_session import v2_document, encode
    from engineering_hub.trusted_queued_session import SCHEMA_V3
    from tests.test_hub_unified_pilot_startup import profile_document
    config = github_configuration()
    config['manifest'] = profile_document()['manifest']
    config.update(database_path='/var/lib/engineering-hub/local-coordinator/hub.sqlite',
                  coordinator_socket_path='/run/hub-browser/hub.sock',
                  executor_socket_path='/run/hub-executor/executor.sock',
                  secret_root='/var/lib/engineering-hub/local-secrets')
    enrollment = v2_document(); enrollment.update(schema=SCHEMA_V3, operator_actor='human:123')
    config_raw, enrollment_raw = canonical(config), encode(enrollment)
    data = trusted_local_profile()
    data['daemon_policy']['engine_id'] = enrollment['launch']['engine_id']
    _swap_mount(data, 'local_config', content_digest=digest_of(config_raw))
    _swap_mount(data, 'local_enrollment', content_digest=digest_of(enrollment_raw))
    signed = core.ExecutorLaunchProfile.model_validate(data)
    assert trusted_local_coordinator_commitment(signed, config_raw, enrollment_raw).raw_digest == digest_of(config_raw)
    for altered in (dict(config, executor_socket_path='/run/elsewhere/executor.sock'),
                    dict(config, public_auth=dict(config['public_auth'], beau_github_id=124))):
        raw = canonical(altered)
        rebound = trusted_local_profile()
        rebound['daemon_policy']['engine_id'] = enrollment['launch']['engine_id']
        _swap_mount(rebound, 'local_config', content_digest=digest_of(raw))
        _swap_mount(rebound, 'local_enrollment', content_digest=digest_of(enrollment_raw))
        with pytest.raises(ValueError):
            trusted_local_coordinator_commitment(core.ExecutorLaunchProfile.model_validate(rebound), raw, enrollment_raw)
    with pytest.raises(ValueError):
        trusted_local_coordinator_commitment(signed, config_raw + b' ', enrollment_raw)
    remapped = trusted_local_profile()
    remapped['daemon_policy']['engine_id'] = enrollment['launch']['engine_id']
    remapped['daemon_policy']['uid_map'] = [[0, 10002, 1], [1, 200001, 65536]]
    remapped['mounts'] = [dict(mount, uid=210001) for mount in remapped['mounts']]
    _swap_mount(remapped, 'local_config', content_digest=digest_of(config_raw))
    _swap_mount(remapped, 'local_enrollment', content_digest=digest_of(enrollment_raw))
    with pytest.raises(ValueError):
        trusted_local_coordinator_commitment(core.ExecutorLaunchProfile.model_validate(remapped), config_raw, enrollment_raw)


def channel_profile():
    return profile(daemon_policy=policy(), command_channel='inherited-seqpacket-v1')


def test_new_keys_leave_legacy_and_v3_bytes_unchanged():
    for data in (profile(), profile(daemon_policy=policy())):
        value = core.ExecutorLaunchProfile.model_validate(data).enrollment_value()
        assert 'role' not in value and 'command_channel' not in value
        assert core.canonical(value) == core.canonical(data)


def test_coordinator_profile_is_signed_and_has_no_engine_authority():
    raw = core.canonical(coordinator_profile())
    parsed = core.verify_profile_bytes(raw, digest_of(raw))
    assert parsed.role == 'coordinator' and parsed.command_channel == 'inherited-seqpacket-v1'
    assert {item.role for item in parsed.mounts} == {'issuer_socket', 'config', 'state', 'secrets'}
    assert core.canonical(parsed.enrollment_value()) == raw


def test_executor_command_channel_is_a_signed_opt_in():
    raw = core.canonical(channel_profile())
    parsed = core.verify_profile_bytes(raw, digest_of(raw))
    assert parsed.role is None and parsed.command_channel == 'inherited-seqpacket-v1'
    assert parsed.digest() != core.ExecutorLaunchProfile.model_validate(profile(daemon_policy=policy())).digest()
    without_policy = channel_profile()
    del without_policy['daemon_policy']
    with pytest.raises((ValueError, RuntimeError)):
        core.ExecutorLaunchProfile.model_validate(without_policy)


def test_executor_endpoint_is_explicit_private_and_preserves_existing_bytes():
    previous = profile(daemon_policy=policy())
    assert core.ExecutorLaunchProfile.model_validate(previous).enrollment_value() == previous
    data = profile(daemon_policy=policy(), executor_endpoint='protected-uds-v1')
    data['mounts'].append(dict(role='executor_endpoint_directory', source=data['fixture_root']+'/endpoint',
        target=core.EXECUTOR_ENDPOINT_TARGET, kind='directory', read_only=False,
        uid=110001, gid=110001, mode=0o700, content_digest=None))
    assert core.ExecutorLaunchProfile.model_validate(data).enrollment_value() == data
    for role, changes in [('executor_endpoint_directory', {'read_only': True}),
                          ('executor_endpoint_directory', {'uid': 10001}),
                          ('executor_endpoint_directory', {'target': '/run/other'})]:
        bad = dict(data); bad['mounts'] = [dict(item) for item in data['mounts']]
        _swap_mount(bad, role, **changes)
        with pytest.raises((ValueError, RuntimeError)): core.ExecutorLaunchProfile.model_validate(bad)
    for bad in (dict(data, executor_endpoint=None), dict(data, executor_endpoint='other'),
                dict(data, command_channel='inherited-seqpacket-v1')):
        with pytest.raises((ValueError, RuntimeError)): core.ExecutorLaunchProfile.model_validate(bad)


def test_ledger_handoff_requires_signed_completion_policy():
    data = gate_profile()
    legacy = core.ExecutorLaunchProfile.model_validate(data)
    assert 'gate_ledger_channel' not in legacy.enrollment_value()
    data['gate_ledger_channel'] = 'supervisor-listener-v1'
    with pytest.raises((ValueError, RuntimeError)):
        core.ExecutorLaunchProfile.model_validate(data)
    data['gate_completion'] = {'candidate_executable_digest': 'sha256:' + 'a' * 64,
        'rollback_executable_digest': 'sha256:' + 'b' * 64,
        'health_contract': 'engineering-hub.health-json-build-commit.v1'}
    parsed = core.ExecutorLaunchProfile.model_validate(data)
    assert parsed.gate_ledger_channel == 'supervisor-listener-v1'
    assert parsed.digest() != legacy.digest()


def _swap_mount(data, role, **fields):
    data['mounts'] = [dict(item, **fields) if item['role'] == role else item for item in data['mounts']]


@pytest.mark.parametrize('change', [
    'executor_user', 'executor_entrypoint', 'no_serve', 'executor_runner', 'engine_socket', 'generations',
    'executor_config_target', 'executor_state_target', 'missing_secrets', 'no_channel', 'null_channel',
    'null_role', 'executor_role', 'no_policy', 'broker', 'unmapped_coordinator',
])
def test_coordinator_profile_refuses_executor_authority_or_ambiguity(change):
    data = coordinator_profile()
    if change == 'executor_user':
        data['user'] = '10001:10001'
    elif change == 'executor_entrypoint':
        data['entrypoint'] = profile()['entrypoint']
    elif change == 'no_serve':
        data['command'] = ['--config', '/operator/coordinator.json']
    elif change == 'executor_runner':
        data['runner_file'] = profile()['runner_file']
    elif change in {'engine_socket', 'generations'}:
        data['mounts'].append(next(item for item in profile()['mounts'] if item['role'] == change))
    elif change == 'executor_config_target':
        _swap_mount(data, 'config', target='/operator/runner.json')
    elif change == 'executor_state_target':
        _swap_mount(data, 'state', target=core.LAUNCH_TARGETS['state'])
    elif change == 'missing_secrets':
        data['mounts'] = [item for item in data['mounts'] if item['role'] != 'secrets']
    elif change == 'no_channel':
        del data['command_channel']
    elif change == 'null_channel':
        data['command_channel'] = None
    elif change == 'null_role':
        data['role'] = None
    elif change == 'executor_role':
        data['role'] = 'executor'
    elif change == 'no_policy':
        del data['daemon_policy']
    elif change == 'broker':
        data['broker_mode'] = 'broker-v1'
    else:
        # Maps the executor's 10001 inside, but not the coordinator's 10002.
        rows = [[0, 10002, 1], [1, 100001, 10001]]
        data['daemon_policy'] = policy(uid_map=rows, gid_map=rows)
    with pytest.raises((ValueError, RuntimeError)):
        core.ExecutorLaunchProfile.model_validate(data)


def test_executor_profile_refuses_coordinator_targets():
    data = profile()
    _swap_mount(data, 'config', target='/operator/coordinator.json')
    with pytest.raises((ValueError, RuntimeError)):
        core.ExecutorLaunchProfile.model_validate(data)
