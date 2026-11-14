"""Pure canonical projection only; no issuer, backend or fencing claim."""
from dataclasses import replace
import json
from uuid import uuid4

import pytest

from hub_container_executor.effect_binding_canonical import (
    EffectBindingCanonicalError, NonsecretEffectBinding, canonical_nonsecret_binding,
)


def fixture():
    return NonsecretEffectBinding(str(uuid4()), str(uuid4()), "create_candidate", 1,
        str(uuid4()), 123, 456, str(uuid4()), "hub-pilot-demo-0123456789ab-candidate",
        None, "sha256:" + "a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64,
        "generation-1", ("HUB_DB", "CODEX_TOKEN", "HUB_BASE_URL"),
        512 * 1024**2, 1000, 128, 64 * 1024**2, None)


def test_canonical_allowlist_is_stable_and_contains_names_only():
    value = fixture()
    raw = canonical_nonsecret_binding(value)
    reordered = replace(value, environment_names=tuple(reversed(value.environment_names)))
    assert canonical_nonsecret_binding(reordered) == raw
    result = json.loads(raw)
    assert result["environment_names"] == ["CODEX_TOKEN", "HUB_BASE_URL", "HUB_DB"]
    assert set(result) == {"schema", "operation_id", "run_id", "kind", "fence", "daemon",
                           "owner_epoch", "resource_name", "container_id", "image_index_digest",
                           "platform_manifest_digest", "image_config_id", "state_generation_id",
                           "environment_names", "runtime", "probe_set_digest"}
    assert b"fake-secret-value" not in raw


@pytest.mark.parametrize("changes", [
    {"environment_names": ("CODEX_TOKEN=fake-secret-value",)},
    {"environment_names": ("CODEX_TOKEN", "CODEX_TOKEN")},
    {"environment_names": ["CODEX_TOKEN"]},
    {"environment_names": ("CODEX_TOKEN\\nLEAK",)},
    {"kind": "create_rollback"}, {"resource_name": "../foreign"},
    {"container_id": "a" * 64}, {"image_config_id": "secret-token"},
    {"daemon_pid": True}, {"fence": True}, {"boot_id": "not-a-uuid"},
    {"memory_bytes": 1}, {"cpu_millicores": True}, {"tmpfs_bytes": 0},
    {"probe_set_digest": "sha256:" + "d" * 64},
])
def test_untrusted_or_secret_shaped_fields_are_rejected(changes):
    with pytest.raises(EffectBindingCanonicalError):
        canonical_nonsecret_binding(replace(fixture(), **changes))


def test_existing_target_is_exact_and_extra_fields_cannot_be_silently_added():
    value = replace(fixture(), kind="start", resource_name=None, container_id="d" * 64)
    assert json.loads(canonical_nonsecret_binding(value))["container_id"] == "d" * 64
    with pytest.raises(EffectBindingCanonicalError):
        canonical_nonsecret_binding({"kind": "start", "environment": {"TOKEN": "secret"}})
    with pytest.raises(TypeError):
        NonsecretEffectBinding(**{**value.__dict__, "extra_secret": "fake-secret-value"})


def test_material_identity_changes_change_canonical_bytes():
    value = fixture()
    raw = canonical_nonsecret_binding(value)
    for changed in (replace(value, fence=2), replace(value, daemon_start_ticks=457),
                    replace(value, image_config_id="sha256:" + "d" * 64),
                    replace(value, state_generation_id="generation-2"),
                    replace(value, memory_bytes=1024 * 1024**2)):
        assert canonical_nonsecret_binding(changed) != raw


def test_probe_set_is_required_and_distinguishes_expectations():
    value = replace(fixture(), kind="probe", resource_name=None, container_id="d" * 64,
                    probe_set_digest="sha256:" + "e" * 64)
    first = canonical_nonsecret_binding(value)
    assert first != canonical_nonsecret_binding(replace(value, probe_set_digest="sha256:" + "f" * 64))
    with pytest.raises(EffectBindingCanonicalError):
        canonical_nonsecret_binding(replace(value, probe_set_digest=None))
