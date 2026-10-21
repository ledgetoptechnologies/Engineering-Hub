"""Native pure shape/chain validation, no Docker or builder-trust substitution."""
import json
from uuid import uuid4

import pytest

from scripts.prepare_hub_unenrolled_record import canonical, prepare_record, sha


CONFIG = "application/vnd.oci.image.config.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"


def metadata():
    config = canonical({"os": "linux", "architecture": "amd64",
                        "config": {"User": "568:568", "Env": []}})
    manifest = canonical({"schemaVersion": 2, "mediaType": MANIFEST,
        "config": {"mediaType": CONFIG, "digest": sha(config), "size": len(config)},
        "layers": []})
    return manifest, config


def inputs(**changes):
    manifest, config = metadata()
    value = dict(source_commit="a" * 40, context_digest="sha256:" + "b" * 64,
        recipe_digest="sha256:" + "c" * 64,
        observations={"base_source_commit": "a" * 40,
                      "fixture_source_commit": "d" * 40, "registry_auth_observed": True,
                      "layer_fetch_history": "unknown", "content_verification": "synthetic only"},
        manifest_raw=manifest, config_raw=config, preparation_id=str(uuid4()))
    value.update(changes)
    return value


def test_exact_native_record_shape_chain_and_honest_preparation_identity():
    args = inputs()
    draft = prepare_record(**args)
    record, observed = json.loads(draft.record), json.loads(draft.inputs)
    assert record["invocation_id"] == args["preparation_id"]
    assert observed["invocation_id_basis"] == "record_preparation_uuid_not_original_build_invocation"
    assert observed["trusted_or_enrolled"] is False
    assert observed["observations"]["fixture_source_commit"] == "d" * 40
    assert record["source"]["commit"] == "a" * 40
    assert record["source"]["build_inputs_digest"] == sha(draft.inputs)
    assert record["builder_id"] == "codex-local-observer-unenrolled-v1"
    assert record["selection"]["runnable_reference"] == sha(args["manifest_raw"])
    assert draft.native_validator_digest.startswith("sha256:")
    assert prepare_record(**args) == draft


@pytest.mark.parametrize("change", [
    {"source_commit": "main"}, {"context_digest": "mutable:latest"},
    {"recipe_digest": "sha256:" + "0" * 63}, {"preparation_id": "claimed-build-id"},
    {"config_raw": canonical({"os": "linux", "architecture": "arm64"})},
])
def test_wrong_source_digest_identity_or_metadata_denied(change):
    with pytest.raises((ValueError, TypeError)):
        prepare_record(**inputs(**change))
