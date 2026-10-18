"""Synthetic build-record vectors: fake trust only, no image or daemon access."""
import hashlib
import json
from uuid import uuid4

import pytest

from engineering_hub.container_pilot_manifest import PilotManifest
from hub_container_executor.build_record import BuildRecordError, verify_image_record
from tests.test_hub_container_pilot_manifest import make_manifest_data

INDEX_TYPE = "application/vnd.oci.image.index.v1+json"
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
CONFIG_TYPE = "application/vnd.oci.image.config.v1+json"
REPOSITORY = "https://github.com/ledgetoptechnologies/Engineering-Hub"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def blob(raw, media_type):
    return {"digest": digest(raw), "media_type": media_type,
            "size_bytes": len(raw)}, raw


def make_record(role="candidate", platform=None):
    platform = platform or {"os": "linux", "architecture": "amd64", "variant": None}
    manifest_data = make_manifest_data()
    manifest_data["runner_binding"]["platform"] = platform
    selected = manifest_data[role]
    config = {
        "architecture": platform["architecture"], "os": platform["os"],
        "variant": platform["variant"],
        "config": {"Entrypoint": ["/usr/local/bin/hub"], "Cmd": ["serve"],
                   "Env": ["PATH=/usr/bin:/bin", "DEFAULT_MODE=fixture-" + role]},
    }
    config_raw = canonical(config)
    config_blob, config_raw = blob(config_raw, CONFIG_TYPE)
    selected["image_config_id"] = config_blob["digest"]
    image_manifest = {
        "schemaVersion": 2,
        "mediaType": MANIFEST_TYPE,
        "config": {"mediaType": CONFIG_TYPE, "digest": config_blob["digest"],
                   "size": config_blob["size_bytes"]},
        "layers": [],
    }
    manifest_raw = canonical(image_manifest)
    manifest_blob, manifest_raw = blob(manifest_raw, MANIFEST_TYPE)
    descriptor_platform = {"os": platform["os"], "architecture": platform["architecture"]}
    if platform["variant"] is not None:
        descriptor_platform["variant"] = platform["variant"]
    index = {
        "schemaVersion": 2,
        "mediaType": INDEX_TYPE,
        "manifests": [{"mediaType": MANIFEST_TYPE,
                       "digest": manifest_blob["digest"], "size": manifest_blob["size_bytes"],
                       "platform": descriptor_platform}],
    }
    index_raw = canonical(index)
    index_blob, index_raw = blob(index_raw, INDEX_TYPE)
    record = {
        "schema_version": "hub_container_build_record_v1",
        "builder_id": "test-enrolled-builder",
        "invocation_id": str(uuid4()),
        "source": {
            "repository": REPOSITORY,
            "commit": selected["source_commit"],
            "build_context_digest": selected["provenance"]["build_context_digest"],
            "build_recipe_digest": selected["provenance"]["build_recipe_digest"],
            "build_inputs_digest": selected["provenance"]["build_inputs_digest"],
        },
        "platform": platform,
        "selection": {
            "runnable_reference": index_blob["digest"],
            "runnable_media_type": INDEX_TYPE,
            "platform_manifest_digest": manifest_blob["digest"],
            "image_config_id": config_blob["digest"],
        },
        "metadata_blobs": [index_blob, manifest_blob, config_blob],
    }
    record_raw = canonical(record)
    manifest_data[role]["provenance"]["attestation_digest"] = digest(record_raw)
    manifest = PilotManifest.from_bytes(canonical(manifest_data))
    return manifest, record_raw, {item["digest"]: raw for item, raw in
                                  ((index_blob, index_raw), (manifest_blob, manifest_raw),
                                   (config_blob, config_raw))}


class FakeStore:
    def __init__(self, values):
        self.values = dict(values)

    def get(self, identity):
        return self.values[identity]


class FakeTrust:
    """Explicit test-only verifier; never represents a real issuer."""
    def __init__(self, accepted=True):
        self.accepted = accepted
        self.calls = []

    def verify_build_record(self, record_bytes, record, manifest, role):
        self.calls.append((record_bytes, role))
        return self.accepted


def verify_fixture(manifest, record_raw, blobs, trust=None, role="candidate"):
    role_selection = getattr(manifest, role)
    records = FakeStore({role_selection.provenance.attestation_digest: record_raw})
    return verify_image_record(manifest, role, records, FakeStore(blobs), trust)


def test_valid_record_checks_canonical_hash_source_and_descriptor_chain():
    manifest, record_raw, blobs = make_record()
    trust = FakeTrust()
    verified = verify_fixture(manifest, record_raw, blobs, trust)
    assert verified.image_config_id == manifest.candidate.image_config_id
    assert verified.platform_manifest_digest.startswith("sha256:")
    assert verified.image_config["config"]["Entrypoint"] == ["/usr/local/bin/hub"]
    assert verified.image_config["config"]["Cmd"] == ["serve"]
    assert trust.calls == [(record_raw, "candidate")]


def test_direct_manifest_runnable_reference_is_same_platform_descriptor_role():
    _manifest, record_raw, blobs = make_record()
    record = json.loads(record_raw)
    record["selection"]["runnable_reference"] = record["selection"]["platform_manifest_digest"]
    record["selection"]["runnable_media_type"] = MANIFEST_TYPE
    record["metadata_blobs"] = [
        item for item in record["metadata_blobs"] if item["media_type"] != INDEX_TYPE
    ]
    direct_raw = canonical(record)
    data = make_manifest_data()
    data["candidate"]["image_config_id"] = record["selection"]["image_config_id"]
    data["candidate"]["provenance"]["attestation_digest"] = digest(direct_raw)
    manifest = PilotManifest.from_bytes(canonical(data))
    direct_blobs = {key: value for key, value in blobs.items()
                    if key != json.loads(record_raw)["selection"]["runnable_reference"]}
    verified = verify_fixture(manifest, direct_raw, direct_blobs, FakeTrust())
    assert verified.runnable_reference == verified.platform_manifest_digest


def test_arm64_variant_is_bound_across_runner_record_descriptor_and_config():
    platform = {"os": "linux", "architecture": "arm64", "variant": "v8"}
    manifest, record_raw, blobs = make_record(platform=platform)
    verified = verify_fixture(manifest, record_raw, blobs, FakeTrust())
    assert verified.platform == manifest.runner_binding.platform

    record = json.loads(record_raw)
    old_root = record["selection"]["runnable_reference"]
    index = json.loads(blobs[old_root])
    index["manifests"][0]["platform"]["variant"] = "v7"
    index_raw = canonical(index)
    new_root = digest(index_raw)
    record["selection"]["runnable_reference"] = new_root
    for item in record["metadata_blobs"]:
        if item["digest"] == old_root:
            item.update(digest=new_root, size_bytes=len(index_raw))
            break
    corrupted_record = canonical(record)
    bad_data = make_manifest_data()
    bad_data["runner_binding"]["platform"] = platform
    bad_data["candidate"]["image_config_id"] = record["selection"]["image_config_id"]
    bad_data["candidate"]["provenance"]["attestation_digest"] = digest(corrupted_record)
    bad_manifest = PilotManifest.from_bytes(canonical(bad_data))
    bad_blobs = dict(blobs)
    bad_blobs.pop(old_root)
    bad_blobs[new_root] = index_raw
    with pytest.raises(BuildRecordError, match="platform or variant mismatch"):
        verify_fixture(bad_manifest, corrupted_record, bad_blobs, FakeTrust())


def test_missing_or_denied_enrolled_verifier_fails_closed():
    manifest, record_raw, blobs = make_record()
    with pytest.raises(BuildRecordError, match="trusted build verifier"):
        verify_fixture(manifest, record_raw, blobs, None)
    with pytest.raises(BuildRecordError, match="approve record"):
        verify_fixture(manifest, record_raw, blobs, FakeTrust(False))


def test_record_hash_must_equal_manifest_attestation_digest():
    manifest, record_raw, blobs = make_record()
    changed = record_raw + b" "
    with pytest.raises(BuildRecordError, match="attestation digest"):
        verify_fixture(manifest, changed, blobs, FakeTrust())


def test_original_blob_corruption_and_descriptor_config_role_mismatch_fail():
    manifest, record_raw, blobs = make_record()
    config_digest = manifest.candidate.image_config_id
    corrupted = dict(blobs)
    corrupted[config_digest] = corrupted[config_digest] + b" "
    with pytest.raises(BuildRecordError, match="metadata blob"):
        verify_fixture(manifest, record_raw, corrupted, FakeTrust())

    record = json.loads(record_raw)
    record["selection"]["image_config_id"] = record["selection"]["platform_manifest_digest"]
    tampered = canonical(record)
    manifest_data = make_manifest_data()
    manifest_data["candidate"]["provenance"]["attestation_digest"] = digest(tampered)
    bad_manifest = PilotManifest.from_bytes(canonical(manifest_data))
    with pytest.raises(BuildRecordError, match="config identity"):
        verify_fixture(bad_manifest, tampered, blobs, FakeTrust())


def test_duplicate_keys_noncanonical_record_and_floats_are_rejected():
    manifest, record_raw, blobs = make_record()
    duplicate = b'{"schema_version":"hub_container_build_record_v1","schema_version":"x"}'
    with pytest.raises(BuildRecordError):
        verify_fixture(manifest, duplicate, blobs, FakeTrust())
    noncanonical = record_raw.replace(b",", b", ", 1)
    noncanonical_data = make_manifest_data()
    noncanonical_data["candidate"]["image_config_id"] = json.loads(record_raw)["selection"]["image_config_id"]
    noncanonical_data["candidate"]["provenance"]["attestation_digest"] = digest(noncanonical)
    noncanonical_manifest = PilotManifest.from_bytes(canonical(noncanonical_data))
    with pytest.raises(BuildRecordError, match="canonical"):
        verify_fixture(noncanonical_manifest, noncanonical, blobs, FakeTrust())

    record = json.loads(record_raw)
    record["metadata_blobs"][0]["size_bytes"] = float(record["metadata_blobs"][0]["size_bytes"])
    float_raw = canonical(record)
    float_data = make_manifest_data()
    float_data["candidate"]["image_config_id"] = json.loads(float_raw)["selection"]["image_config_id"]
    float_data["candidate"]["provenance"]["attestation_digest"] = digest(float_raw)
    float_manifest = PilotManifest.from_bytes(canonical(float_data))
    with pytest.raises(BuildRecordError, match="numbers must be integers"):
        verify_fixture(float_manifest, float_raw, blobs, FakeTrust())
