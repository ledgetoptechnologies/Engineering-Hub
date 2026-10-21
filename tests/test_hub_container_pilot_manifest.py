"""Pure synthetic operator-manifest vectors; no Docker or credential resolver."""
import copy
import hashlib
import json

import pytest
from pydantic import ValidationError

from engineering_hub.container_pilot_manifest import MAX_BYTES, MIN_CGROUPNS_ENGINE_API, PilotManifest, PROBE_PROFILE


def make_manifest_data():
    def digest(char):
        return "sha256:" + char * 64

    def image(char):
        return {
            "image_config_id": digest(char),
            "source_repository": "https://github.com/ledgetoptechnologies/Engineering-Hub",
            "source_commit": char * 40,
            "provenance": {key: digest(char) for key in (
                "build_context_digest", "build_recipe_digest", "build_inputs_digest", "attestation_digest")},
        }

    probes = [{"kind": kind, "method": "GET", "path": path, "expected_status": status,
               "expectation_id": expectation, "max_response_bytes": 1024 * 1024, "timeout_ms": 1000}
              for kind, path, status, expectation in PROBE_PROFILE]
    return {
        "schema_version": "container_pilot_manifest_v1",
        "driver": {"id": "container_pilot_v1", "implementation_commit": "c" * 40},
        "campaign_id": "1398cf6d-cebd-4f79-a913-be70321970c9",
        "target_id": "engineering-hub:staging", "policy_revision": 1,
        "runner_binding": {"runner_id": "fixture-runner", "daemon_identity": "fixture-daemon",
                           "platform": {"os": "linux", "architecture": "amd64", "variant": None}},
        "candidate": image("a"), "rollback": image("b"),
        "runtime": {
            "resource_name_prefix": "hub-pilot-fixture", "user": {"uid": 10001, "gid": 10001, "supplementary_groups": []},
            "read_only_root": True, "cap_drop": ["ALL"], "no_new_privileges": True, "privileged": False,
            "pid_mode": "private", "ipc_mode": "private", "cgroupns_mode": "private",
            "network": {"mode": "none", "policy_digest": digest("d"), "loopback_address": None,
                        "host_port": None, "container_port": 8770},
            "resources": {"memory_bytes": 512 * 1024**2, "cpu_millicores": 1000,
                          "pids_limit": 128, "scratch_bytes": 64 * 1024**2},
            "tmpfs": {"/tmp": {"size_bytes": 64 * 1024**2, "mode": 1777, "noexec": True, "nosuid": True}},
            "state_mount": {"container_destination": "/var/lib/hub", "mode": "rw"},
            "environment": {"HUB_BASE_URL": "http://127.0.0.1:8770", "HUB_DB": "/var/lib/hub/hub.sqlite3"},
            "secret_bindings": {name + "_TOKEN": {"slot_id": "test-" + name.lower(), "version": 1, "test_only": True}
                                for name in ("CODEX", "HERMES", "CLAUDE")},
            "entrypoint_policy": "image_default", "command_policy": "image_default", "restart_policy": "no",
        },
        "state_policy": {"mode": "generation_copy", "baseline_id": "fixture-baseline", "baseline_backup_digest": digest("e"),
                         "no_external_writes": True, "allow_migrations": False, "retain_previous_unchanged": True,
                         "rollback_mode": "restore_previous_generation"},
        "probes": {"candidate": probes, "rollback": copy.deepcopy(probes)},
        "timeouts": {"create_ms": 10000, "start_ms": 10000, "stop_ms": 10000, "inspect_ms": 10000, "total_command_ms": 30000},
        "failure_policy": {"known_failure": "rollback_then_verify", "unknown": "hold_inspect_only",
                           "rollback_verification_failure": "hold_reserved", "pause": "deny_new_effects"},
        "retention": {"max_generations": 3, "max_storage_bytes": 1024**3,
                      "keep_previous_until_verified": True, "foreign_resource_policy": "refuse"},
    }


def parse(data):
    return PilotManifest.from_bytes(json.dumps(data).encode())


def set_path(data, path, value):
    keys = path.split(".")
    cursor = data
    for key in keys[:-1]:
        cursor = cursor[int(key)] if isinstance(cursor, list) else cursor[key]
    cursor[int(keys[-1]) if isinstance(cursor, list) else keys[-1]] = value


def test_positive_canonical_vector():
    data = make_manifest_data()
    manifest = parse(data)
    expected = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    assert manifest.canonical_bytes() == expected
    assert manifest.digest() == "sha256:" + hashlib.sha256(expected).hexdigest()
    assert manifest.digest() == "sha256:e984626ae49fb99716d41cbab2e2cafcf356ef27bcc81a48f720731c5c752bc7"
    assert PilotManifest.from_bytes(expected).canonical_bytes() == expected
    assert parse(dict(reversed(list(data.items())))).digest() == manifest.digest()


def test_zero_startup_grace_preserves_legacy_bytes_and_probe_digest():
    from hub_container_executor.runtime_profile import probe_set_digest
    data = make_manifest_data()
    legacy = parse(data)
    expected_probes = json.dumps(data['probes']['candidate'], sort_keys=True,
                                separators=(',', ':')).encode()
    for role in ('candidate', 'rollback'):
        for probe in data['probes'][role]:
            probe['startup_grace_ms'] = 0
    explicit_zero = parse(data)
    assert explicit_zero.canonical_bytes() == legacy.canonical_bytes()
    assert explicit_zero.digest() == 'sha256:e984626ae49fb99716d41cbab2e2cafcf356ef27bcc81a48f720731c5c752bc7'
    assert probe_set_digest(explicit_zero.probes.candidate) == 'sha256:' + hashlib.sha256(expected_probes).hexdigest()
    assert all('startup_grace_ms' not in row for row in explicit_zero.model_dump(mode='json')['probes']['candidate'])


def test_positive_health_startup_grace_is_explicit_canonical_policy_and_budget():
    from hub_container_executor.runtime_profile import probe_set_digest
    data = make_manifest_data()
    data['probes']['candidate'][0]['startup_grace_ms'] = 30000
    with pytest.raises(ValueError):
        parse(data)  # 3s ordinary probes +30s grace exceeds30s command.
    data['timeouts']['total_command_ms'] = 33000
    manifest = parse(data)
    assert manifest.probes.candidate[0].startup_grace_ms == 30000
    assert PilotManifest.from_bytes(manifest.canonical_bytes()).canonical_bytes() == manifest.canonical_bytes()
    assert probe_set_digest(manifest.probes.candidate) != probe_set_digest(manifest.probes.rollback)
    assert manifest.model_dump(mode='json')['probes']['candidate'][0]['startup_grace_ms'] == 30000


def test_zero_grace_serializer_respects_normal_pydantic_exclusion_options():
    probe = parse(make_manifest_data()).probes.candidate[0]
    assert 'startup_grace_ms' not in probe.model_dump(exclude_defaults=True)
    assert 'startup_grace_ms' not in probe.model_dump(exclude_unset=True)
    assert 'startup_grace_ms' not in probe.model_dump(exclude={'startup_grace_ms'})
    assert probe.model_dump(include={'kind'}) == {'kind': 'health_json'}


@pytest.mark.parametrize('value', [True, False, 0.0, 1.0, None, -1, 30001, '1'])
def test_invalid_startup_grace_is_never_hidden_by_serialization(value):
    data = make_manifest_data()
    data['probes']['candidate'][0]['startup_grace_ms'] = value
    with pytest.raises(ValueError):
        parse(data)
    manifest = parse(make_manifest_data())
    bad_probe = manifest.probes.candidate[0].model_copy(update={'startup_grace_ms': value})
    bad_probes = manifest.probes.model_copy(update={'candidate': (bad_probe, *manifest.probes.candidate[1:])})
    with pytest.raises(ValueError):
        manifest.model_copy(update={'probes': bad_probes}).canonical_bytes()


@pytest.mark.parametrize('index', [1, 2])
def test_startup_grace_cannot_apply_to_assets_or_auth(index):
    data = make_manifest_data()
    data['probes']['candidate'][index]['startup_grace_ms'] = 1
    with pytest.raises(ValueError):
        parse(data)
    manifest = parse(make_manifest_data())
    probes = list(manifest.probes.candidate)
    probes[index] = probes[index].model_copy(update={'startup_grace_ms': 1})
    with pytest.raises(ValueError):
        manifest.model_copy(update={'probes': manifest.probes.model_copy(update={'candidate': tuple(probes)})}).canonical_bytes()


@pytest.mark.parametrize("path,value", [
    ("policy_revision", True), ("policy_revision", 2), ("policy_revision", 1.0), ("policy_revision", "1"),
    ("campaign_id", "1398CF6D-CEBD-4F79-A913-BE70321970C9"), ("campaign_id", "not-a-uuid"),
    ("candidate.image_config_id", "a" * 40), ("candidate.source_commit", "sha256:" + "a" * 64),
    ("candidate.source_commit", "A" * 40), ("candidate.source_repository", "https://evil.test/repo"),
    ("candidate.provenance.attestation_digest", "sha256:" + "G" * 64),
    ("runner_binding.platform.os", "windows"), ("runner_binding.platform.variant", "v8"),
    ("runtime.user.uid", True), ("runtime.user.uid", 0), ("runtime.user.supplementary_groups", [0]),
    ("runtime.cap_drop", []), ("runtime.cap_drop", ["ALL", "ALL"]), ("runtime.cap_drop", ["SYS_ADMIN"]),
    ("runtime.cgroupns_mode", "host"), ("runtime.cgroupns_mode", ""), ("runtime.cgroupns_mode", None),
    ("runtime.cgroupns_mode", True), ("runtime.cgroupns_mode", "PRIVATE"),
    ("runtime.network.mode", "bridge"), ("runtime.network.host_port", 8770),
    ("runtime.network.container_port", True), ("runtime.network.loopback_address", "127.0.0.1"),
    ("runtime.resources.memory_bytes", 128 * 1024**2 - 1), ("runtime.resources.memory_bytes", 2 * 1024**3 + 1),
    ("runtime.resources.cpu_millicores", True), ("runtime.resources.cpu_millicores", 2001),
    ("runtime.resources.pids_limit", 0), ("runtime.resources.scratch_bytes", 1024**2),
    ("runtime.tmpfs./tmp.mode", 1023), ("runtime.state_mount.container_destination", "/var/run/docker.sock"),
    ("runtime.environment.HUB_BASE_URL", "http://evil.test"),
    ("runtime.secret_bindings.CODEX_TOKEN.slot_id", "production-token"),
    ("runtime.secret_bindings.CODEX_TOKEN.version", "latest"), ("runtime.secret_bindings.CODEX_TOKEN.version", True),
    ("runtime.secret_bindings.CODEX_TOKEN.slot_id", "test-hermes"),
    ("runtime.resource_name_prefix", "../../outside"), ("runtime.resource_name_prefix", "hub-pilot-" + "x" * 50),
    ("runtime.restart_policy", "always"), ("runtime.command_policy", "sh -c anything"),
    ("state_policy.baseline_id", "../../outside"), ("probes.candidate.0.path", "http://evil.test/health"),
    ("probes.candidate.0.expected_status", True), ("probes.candidate.0.expected_status", 401),
    ("probes.candidate.0.expectation_id", "hub_auth_denied_v1"), ("probes.candidate.0.timeout_ms", 10001),
    ("probes.candidate.0.max_response_bytes", 2 * 1024**2 + 1),
    ("timeouts.create_ms", 30001), ("timeouts.total_command_ms", 120001), ("timeouts.total_command_ms", 2000),
    ("retention.max_generations", 1), ("retention.max_storage_bytes", 8 * 1024**3 + 1),
    ("failure_policy.unknown", "retry"),
])
def test_rejects_unsupported_profile(path, value):
    data = make_manifest_data()
    set_path(data, path, value)
    with pytest.raises(ValueError):
        parse(data)


def test_private_cgroup_namespace_is_required_not_a_daemon_default():
    data = make_manifest_data()
    assert MIN_CGROUPNS_ENGINE_API == (1, 41)
    manifest = parse(data)
    assert manifest.runtime.cgroupns_mode == 'private'
    del data['runtime']['cgroupns_mode']
    with pytest.raises(ValueError):
        parse(data)
    bypass = manifest.model_copy(update={
        'runtime': manifest.runtime.model_copy(update={'cgroupns_mode': 'host'})})
    with pytest.raises(ValueError):
        bypass.canonical_bytes()


BOOLEAN_PATHS = [
    "runtime.read_only_root", "runtime.no_new_privileges", "runtime.privileged", "runtime.tmpfs./tmp.noexec",
    "runtime.tmpfs./tmp.nosuid", "runtime.secret_bindings.CODEX_TOKEN.test_only", "state_policy.no_external_writes",
    "state_policy.allow_migrations", "state_policy.retain_previous_unchanged", "retention.keep_previous_until_verified",
]


@pytest.mark.parametrize("path", BOOLEAN_PATHS)
@pytest.mark.parametrize("numeric", [0, 1])
def test_fixed_booleans_reject_integer_aliases(path, numeric):
    data = make_manifest_data()
    set_path(data, path, numeric)
    with pytest.raises(ValueError):
        parse(data)


def walk_objects(data, path=()):
    if isinstance(data, dict):
        yield path
        for key, value in data.items():
            yield from walk_objects(value, path + (key,))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            yield from walk_objects(value, path + (index,))


def at(data, path):
    for key in path:
        data = data[key]
    return data


@pytest.mark.parametrize("path", list(walk_objects(make_manifest_data())))
def test_every_shape_closed_and_required(path):
    data = make_manifest_data()
    at(data, path)["unexpected"] = "not-allowed"
    with pytest.raises(ValueError):
        parse(data)
    original = at(make_manifest_data(), path)
    for key in original:
        missing = make_manifest_data()
        del at(missing, path)[key]
        with pytest.raises(ValueError):
            parse(missing)


@pytest.mark.parametrize("raw", [
    b'{"schema_version":"a","schema_version":"b"}',
    b'{"nested":{"key":1,"key":2}}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}',
    b'{"x":1.0}', b'{"x":1e0}', b'{"x":"\xff"}', b'{"x":"\\ud800"}',
    b"[]", b"null", b"{}", b"{" * 1000, b" " * (MAX_BYTES + 1),
], ids=["duplicate-root", "duplicate-nested", "nan", "inf", "negative-inf", "float", "exponent", "non-utf8",
        "surrogate", "array", "null", "empty", "nesting", "oversize"])
def test_wire_rejects_malformed_input(raw):
    with pytest.raises(ValueError):
        PilotManifest.from_bytes(raw)


def test_unicode_is_not_normalized_or_repaired():
    for text in ("é", "e\u0301", "\x00"):
        data = make_manifest_data()
        data["runner_binding"]["runner_id"] = text
        with pytest.raises(ValueError):
            parse(data)


def test_frozen_deep_model_and_detached_input():
    data = make_manifest_data()
    manifest = parse(data)
    original = manifest.digest()
    data["runtime"]["resources"]["memory_bytes"] = 1
    assert manifest.digest() == original
    with pytest.raises(ValidationError):
        manifest.runtime.resources.memory_bytes = 1
    with pytest.raises(TypeError):
        manifest.runtime.cap_drop[0] = "SYS_ADMIN"
    dumped = manifest.model_dump(mode="json", by_alias=True)
    dumped["runtime"]["secret_bindings"]["CODEX_TOKEN"]["version"] = 2
    assert manifest.digest() == original


def test_copy_bypass_cannot_be_canonicalized():
    manifest = parse(make_manifest_data())
    invalid = manifest.model_copy(update={"runtime": manifest.runtime.model_copy(update={"privileged": True})})
    with pytest.raises(ValueError):
        invalid.canonical_bytes()
    invalid_extra = manifest.model_copy(update={"candidate": {**make_manifest_data()["candidate"], "extra": 1}})
    with pytest.raises(ValueError):
        invalid_extra.canonical_bytes()
    hidden_extra = manifest.model_copy(update={"unserialized_authority": True})
    with pytest.raises(ValueError):
        hidden_extra.canonical_bytes()
    nested_extra = manifest.model_copy(update={"runtime": manifest.runtime.model_copy(update={"unexpected": True})})
    with pytest.raises(ValueError):
        nested_extra.canonical_bytes()


def test_wire_limit_type_and_explicit_nulls():
    raw = json.dumps(make_manifest_data()).encode()
    assert PilotManifest.from_bytes(raw + b" " * (MAX_BYTES - len(raw)))
    for invalid in (raw + b" " * (MAX_BYTES - len(raw) + 1), raw.decode(), bytearray(raw)):
        with pytest.raises(ValueError):
            PilotManifest.from_bytes(invalid)
    data = make_manifest_data()
    del data["runner_binding"]["platform"]["variant"]
    with pytest.raises(ValueError):
        parse(data)


@pytest.mark.parametrize("path,value", [
    ("driver.implementation_commit", "d" * 40), ("campaign_id", "2398cf6d-cebd-4f79-a913-be70321970c9"),
    ("runner_binding.runner_id", "runner-2"), ("runner_binding.daemon_identity", "daemon-2"),
    ("candidate.image_config_id", "sha256:" + "f" * 64), ("rollback.source_commit", "f" * 40),
    ("candidate.provenance.build_context_digest", "sha256:" + "f" * 64),
    ("candidate.provenance.build_recipe_digest", "sha256:" + "f" * 64),
    ("candidate.provenance.build_inputs_digest", "sha256:" + "f" * 64),
    ("candidate.provenance.attestation_digest", "sha256:" + "f" * 64),
    ("runtime.resources.cpu_millicores", 1001), ("runtime.network.policy_digest", "sha256:" + "f" * 64),
    ("runtime.secret_bindings.CODEX_TOKEN.version", 2), ("runtime.resource_name_prefix", "hub-pilot-other"),
    ("state_policy.baseline_id", "new-baseline"), ("state_policy.baseline_backup_digest", "sha256:" + "f" * 64),
    ("probes.rollback.0.timeout_ms", 1001), ("timeouts.inspect_ms", 10001),
    ("retention.max_generations", 4), ("retention.max_storage_bytes", 2 * 1024**3),
])
def test_material_changes_alter_digest(path, value):
    data = make_manifest_data()
    initial = parse(data).digest()
    set_path(data, path, value)
    assert parse(data).digest() != initial


def test_probe_arrays_order_and_completeness_are_bound():
    data = make_manifest_data()
    data["probes"]["candidate"].reverse()
    with pytest.raises(ValueError):
        parse(data)
    data = make_manifest_data()
    data["probes"]["rollback"].pop()
    with pytest.raises(ValueError):
        parse(data)


def test_arm64_explicit_variant_supported():
    data = make_manifest_data()
    data["runner_binding"]["platform"] = {"os": "linux", "architecture": "arm64", "variant": "v8"}
    assert parse(data).runner_binding.platform.variant == "v8"


def test_distinct_image_selections_and_wire_mode():
    data = make_manifest_data()
    assert parse(data).runtime.tmpfs.tmp.mode == 1777  # Wire octal digits, not decimal bitmask 1023.
    data["rollback"]["image_config_id"] = data["candidate"]["image_config_id"]
    with pytest.raises(ValueError):
        parse(data)
