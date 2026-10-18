"""Pure profile compatibility; synthetic verified records, no Engine calls."""
from dataclasses import asdict, replace
import hashlib
import json

import pytest

pytest.importorskip('fcntl', reason='Optional Linux host package')

from hub_container_executor.adapter import HostAdapter
from hub_container_executor.runtime_profile import (
    derive_runtime_profile, probe_set_digest, runtime_profile_digest,
)
from tests.test_hub_container_executor_daemon_backend import verified_image


def test_shared_profile_preserves_closed_runtime_and_legacy_adapter_shape():
    manifest, image, _ = verified_image('rollback')
    defaults = HostAdapter._defaults_from_record(image)
    values = {'CODEX_TOKEN': 'synthetic-codex', 'HERMES_TOKEN': 'synthetic-hermes',
              'CLAUDE_TOKEN': 'synthetic-claude'}
    profile = derive_runtime_profile(manifest, defaults, 'baseline-g1', values)
    runtime = manifest.runtime
    assert profile.uid == profile.gid == 10001
    assert profile.read_only_root and profile.cap_drop == ('ALL',)
    assert profile.no_new_privileges and not profile.privileged
    assert profile.network_mode == 'none' and profile.published_ports == ()
    assert profile.pid_mode == profile.ipc_mode == profile.cgroupns_mode == 'private'
    assert profile.state_generation_id == 'baseline-g1'
    assert profile.state_destination == '/var/lib/hub' and profile.state_mode == 'rw'
    assert profile.memory_bytes == runtime.resources.memory_bytes
    assert profile.cpu_millicores == runtime.resources.cpu_millicores
    assert profile.pids_limit == runtime.resources.pids_limit
    assert profile.restart_policy == 'no'
    assert profile.entrypoint == defaults.entrypoint and profile.command == defaults.command
    assert profile.tmpfs == (('/tmp', runtime.tmpfs.tmp.size_bytes, 0o1777, True, True),)
    assert profile.mounts == (
        ('state_generation', '/var/lib/hub', 'rw', 'baseline-g1'),
        ('tmpfs', '/tmp', 'rw', f'size={runtime.tmpfs.tmp.size_bytes}', 'mode=1777', 'noexec', 'nosuid'))
    environment = dict(item.split('=', 1) for item in defaults.environment)
    environment.update(values)
    environment.update(HUB_BASE_URL='http://127.0.0.1:8770', HUB_DB='/var/lib/hub/hub.sqlite3')
    assert profile.environment == tuple(sorted(environment.items()))
    # Exercise the existing wrapper without constructing a ledger or granting
    # any driver authority. _profile only reads this explicit manifest.
    class Context:
        pass
    context = Context()
    context.manifest = manifest
    assert HostAdapter._profile(context, image, defaults, 'baseline-g1', values) == profile


def test_probe_digest_stays_compatible_but_is_not_runtime_profile_digest():
    manifest, image, _ = verified_image('rollback')
    probes = manifest.probes.rollback
    raw = json.dumps([p.model_dump(mode='json') for p in probes],
                     sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    expected = 'sha256:' + hashlib.sha256(raw).hexdigest()
    assert probe_set_digest(probes) == HostAdapter._probe_digest(probes) == expected
    profile = derive_runtime_profile(manifest, HostAdapter._defaults_from_record(image), 'baseline-g1', {})
    assert runtime_profile_digest(profile) != expected


def test_private_profile_digest_binds_resources_generation_and_environment():
    manifest, image, _ = verified_image('rollback')
    profile = derive_runtime_profile(manifest, HostAdapter._defaults_from_record(image), 'baseline-g1', {})
    digest = runtime_profile_digest(profile)
    raw = json.dumps(asdict(profile), sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    assert digest == 'sha256:' + hashlib.sha256(raw).hexdigest()
    assert runtime_profile_digest(replace(profile, memory_bytes=profile.memory_bytes + 1)) != digest
    assert runtime_profile_digest(replace(profile, state_generation_id='different')) != digest
    assert runtime_profile_digest(replace(profile, environment=profile.environment + (('TEST', 'changed'),))) != digest
    with pytest.raises(ValueError, match='Exact effective runtime profile required'):
        runtime_profile_digest(asdict(profile))
