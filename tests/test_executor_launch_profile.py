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
                      'hub_container_executor import', 'from hub_container_executor', 'open('):
        assert forbidden not in text, forbidden
