"""Root-private policy tests; these do not prove host or operator approval."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import pytest

pytest.importorskip('fcntl')
from hub_container_executor import daemon_scope_policy as module


def payload():
    return {'schema': 'engineering-hub.daemon-scope-policy.v1', 'scope': 'system.slice/example.service',
            'scope_device': 1, 'scope_inode': 123, 'daemon_pid': 1234, 'daemon_start_ticks': 99,
            'daemon_id': 'synthetic-daemon', 'boot_id': module._boot_id(), 'wait_seconds': 5}


def write(path, value):
    path.write_text(json.dumps(value, sort_keys=True, separators=(',', ':')))
    path.chmod(0o600)


def test_policy_is_exact_root_private_and_rechecked(tmp_path):
    if os.getuid() != 0: pytest.skip('root-private policy test')
    root = tmp_path / 'root'
    root.mkdir(mode=0o700)
    path = root / 'policy.json'
    value = payload()
    write(path, value)
    policy = module.load_policy(str(path))
    process = SimpleNamespace(pid=1234, start=99, boot=value['boot_id'])
    daemon = SimpleNamespace(_daemon_pin=SimpleNamespace(pid=1234, start_ticks=99,
        daemon_id='synthetic-daemon'), _engine=SimpleNamespace(process=process))
    scope = SimpleNamespace(_scope=value['scope'], _identity=(1, 123), _processes=(process,),
                            _allow_descendants=False)
    policy.check(daemon, scope)
    scope._identity = (1, 124)
    with pytest.raises(module.DaemonScopePolicyError): policy.check(daemon, scope)
    scope._identity = (1, 123)
    write(path, value | {'daemon_id': 'another-daemon'})
    with pytest.raises(module.DaemonScopePolicyError): policy.check(daemon, scope)


@pytest.mark.parametrize('key,value', [('scope', '../unsafe'), ('wait_seconds', 0), ('wait_seconds', 3601),
    ('daemon_pid', True), ('scope_inode', 0), ('boot_id', 'not-uuid'), ('daemon_id', '')])
def test_policy_rejects_invalid_or_unbound_fields(tmp_path, key, value):
    if os.getuid() != 0: pytest.skip('root-private policy test')
    root = tmp_path / 'root'
    root.mkdir(mode=0o700)
    path = root / 'policy.json'
    write(path, payload() | {key: value})
    with pytest.raises(module.DaemonScopePolicyError): module.load_policy(str(path))


def test_worker_policy_pins_exact_inventory_and_mode(tmp_path):
    if os.getuid() != 0: pytest.skip('root-private policy test')
    root = tmp_path / 'root'
    root.mkdir(mode=0o700)
    path = root / 'policy.json'
    value = payload() | {'schema':'engineering-hub.daemon-scope-policy.v2',
                         'members':[{'pid': 2345, 'start_ticks': 77},
                                    {'pid': 1234, 'start_ticks': 99}]}
    write(path,value)
    policy = module.load_policy(str(path))
    assert policy.members == ((1234,99),(2345,77))
    process = SimpleNamespace(pid=1234,start=99,boot=value['boot_id'])
    worker = SimpleNamespace(pid=2345,start=77,boot=value['boot_id'])
    supervisor = SimpleNamespace(_daemon_pin=SimpleNamespace(pid=1234,start_ticks=99,
        daemon_id=value['daemon_id']),_engine=SimpleNamespace(process=process))
    scope = SimpleNamespace(_scope=value['scope'],_identity=(1,123),_processes=(process,worker),
                            _allow_descendants=True,_original=((1234,99),(2345,77)))
    policy.check(supervisor,scope)
    scope._original = ((1234,99),)
    with pytest.raises(module.DaemonScopePolicyError): policy.check(supervisor,scope)
    scope._original = policy.members
    scope._allow_descendants = False
    with pytest.raises(module.DaemonScopePolicyError): policy.check(supervisor,scope)


@pytest.mark.parametrize('members', [[], [{'pid':1234,'start_ticks':98}],
    [{'pid':1234,'start_ticks':99},{'pid':1234,'start_ticks':99}],
    [{'pid':1234,'start_ticks':99},{'pid':True,'start_ticks':2}]])
def test_worker_policy_rejects_incomplete_or_ambiguous_inventory(tmp_path, members):
    if os.getuid() != 0: pytest.skip('root-private policy test')
    root=tmp_path/'root'; root.mkdir(mode=0o700); path=root/'policy.json'
    write(path,payload() | {'schema':'engineering-hub.daemon-scope-policy.v2','members':members})
    with pytest.raises(module.DaemonScopePolicyError): module.load_policy(str(path))
