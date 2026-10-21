"""Synthetic full-blob OCI verification, never real build/runtime acceptance."""
from copy import deepcopy
import gzip
import hashlib
import io
import json
import tarfile

import pytest

from scripts.inspect_hub_image_identity import IdentityError
from scripts.prepare_hub_rollback_fault import _FAULT_SCRIPT
from scripts import verify_hub_rollback_fault_content as verifier


SOURCE = "b" * 40
STAMP = "2026-10-21T18:00:00Z"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"
RAW = "application/vnd.oci.image.layer.v1.tar"
GZIP = RAW + "+gzip"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def tar(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for entry in entries:
            name, raw, kind = entry[:3]
            member = tarfile.TarInfo(name)
            member.type, member.mode, member.mtime = kind, 0o755 if kind == tarfile.DIRTYPE else 0o644, 0
            member.size = len(raw)
            for key, value in (entry[3] if len(entry) == 4 else {}).items():
                setattr(member, key, value)
            if kind == tarfile.SYMTYPE:
                member.linkname = "/foreign"
            archive.addfile(member, io.BytesIO(raw))
    return stream.getvalue()


def fault_tar(extra=()):
    return tar([("app", b"", tarfile.DIRTYPE), ("app/dashboard", b"", tarfile.DIRTYPE),
                ("app/dashboard/.wh.index.html", b"", tarfile.REGTYPE), *extra])


def config(diff_ids, *, child=False):
    value = {"architecture": "amd64", "os": "linux", "created": STAMP,
        "config": {"User": "568:568", "Env": ["PATH=/opt/venv/bin:/usr/bin:/bin"],
            "Entrypoint": ["python", "-m", "engineering_hub"], "Cmd": [], "WorkingDir": "/app",
            "Labels": {"org.opencontainers.image.revision": SOURCE}},
        "rootfs": {"type": "layers", "diff_ids": diff_ids},
        "history": [{"created": STAMP, "created_by": "base application"}]}
    if child:
        value["config"]["Labels"]["engineering-hub.test-only"] = "rollback-missing-index-v1"
        command = ["/opt/venv/bin/python", "-I", "-B", "-c", _FAULT_SCRIPT, "/app/dashboard"]
        value["history"] += [
            {"created": STAMP, "created_by": "USER 0:0", "empty_layer": True},
            {"created": STAMP, "created_by": "RUN " + " ".join(command) + " # buildkit"},
            {"created": STAMP, "created_by": "USER 568:568", "empty_layer": True},
            {"created": STAMP, "created_by": "LABEL engineering-hub.test-only=rollback-missing-index-v1",
             "empty_layer": True}]
    return value


def image(layers, cfg, *, bad_blob=None, layer_media=None, manifest_change=None):
    blobs = {sha(raw): raw for raw in layers}
    config_raw = canonical(cfg)
    blobs[sha(config_raw)] = config_raw
    layer_media = layer_media or [RAW] * len(layers)
    manifest = {"schemaVersion": 2, "mediaType": MANIFEST,
        "config": {"mediaType": CONFIG, "digest": sha(config_raw), "size": len(config_raw)},
        "layers": [{"mediaType": media, "digest": sha(raw), "size": len(raw)}
                   for raw, media in zip(layers, layer_media)]}
    if manifest_change:
        manifest_change(manifest)
    manifest_raw = canonical(manifest)
    reference = sha(manifest_raw)
    blobs[reference] = manifest_raw
    if bad_blob:
        blobs[bad_blob[0]] = bad_blob[1]
    index = canonical({"schemaVersion": 2, "manifests": [{"mediaType": MANIFEST,
        "digest": reference, "size": len(manifest_raw),
        "platform": {"os": "linux", "architecture": "amd64"}}]})
    # Put blobs before metadata and manifest to exercise actual streaming order.
    archive = tar([*(('blobs/sha256/' + key[7:], raw, tarfile.REGTYPE)
                     for key, raw in blobs.items()),
                   ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}', tarfile.REGTYPE),
                   ("index.json", index, tarfile.REGTYPE)])
    return archive, reference


def fixture(*, compressed=False, base_compressed=False, extra=(), change=None,
            child_base=None, bad_blob=None, manifest_change=None, encoding_change=None):
    base_raw = tar([("app/dashboard/index.html", b"original index", tarfile.REGTYPE)])
    base_blob = gzip.compress(base_raw, mtime=0) if base_compressed else base_raw
    raw_fault = fault_tar(extra)
    fault = gzip.compress(raw_fault, mtime=0) if compressed else raw_fault
    if encoding_change:
        fault = encoding_change(fault)
    base_config = config([sha(base_raw)])
    child_config = config([sha(base_raw), sha(raw_fault)], child=True)
    if change:
        change(child_config)
    base_media = GZIP if base_compressed else RAW
    base_archive, base_reference = image([base_blob], base_config, layer_media=[base_media])
    child_archive, child_reference = image([child_base or base_blob, fault], child_config,
        bad_blob=bad_blob, layer_media=[base_media, GZIP if compressed else RAW],
        manifest_change=manifest_change)
    arguments = dict(base_reference=base_reference, fault_reference=child_reference,
        expected_fault_layer=sha(fault), platform="linux/amd64", base_source_commit=SOURCE)
    return base_archive, child_archive, arguments


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("base_compressed", [False, True])
def test_exact_content_chain_and_only_whiteout_pass(compressed, base_compressed):
    base, child, args = fixture(compressed=compressed, base_compressed=base_compressed)
    result = verifier.verify_fault_archive(io.BytesIO(base), io.BytesIO(child), **args)
    assert result["verified_fault"] == "missing_dashboard_index"
    assert result["runtime_acceptance"] is False
    assert result["registry_contact"] == result["builder_provenance"] == "not_established"


@pytest.mark.parametrize("extra", [
    [("usr/local/lib/python3.12/__pycache__/pathlib.pyc", b"unrelated pyc", tarfile.REGTYPE)],
    [("app/dashboard/.wh..wh..opq", b"", tarfile.REGTYPE)],
    [("app/dashboard/link", b"", tarfile.SYMTYPE)],
    [("../escape", b"", tarfile.REGTYPE)],
    [("app/dashboard/.wh.index.html", b"", tarfile.REGTYPE)],
])
def test_extra_opaque_symlink_traversal_or_duplicate_entries_denied(extra):
    base, child, args = fixture(extra=extra)
    with pytest.raises(IdentityError):
        verifier.verify_fault_archive(io.BytesIO(base), io.BytesIO(child), **args)


@pytest.mark.parametrize("change", [
    lambda c: c["config"].update(User="0:0"),
    lambda c: c["config"].update(Env=["UNAPPROVED=1"]),
    lambda c: c["config"].update(Entrypoint=["sh"]),
    lambda c: c["config"]["Labels"].update({"org.opencontainers.image.revision": "d" * 40}),
    lambda c: c.update(unapproved="new unchecked field"),
    lambda c: c["history"][-3].update(created_by="RUN arbitrary # buildkit"),
    lambda c: c["history"][-3].update(extra="unapproved"),
    lambda c: c["history"][-3].update(empty_layer=True),
    lambda c: c.update(created="not a timestamp"),
    lambda c: c["rootfs"]["diff_ids"].__setitem__(-1, "sha256:" + "a" * 64),
])
def test_changed_config_history_source_or_diffid_denied(change):
    base, child, args = fixture(change=change)
    with pytest.raises(IdentityError):
        verifier.verify_fault_archive(io.BytesIO(base), io.BytesIO(child), **args)


def test_changed_base_layer_denied():
    base, child, args = fixture(child_base=tar([("foreign", b"bad", tarfile.REGTYPE)]))
    with pytest.raises(IdentityError):
        verifier.verify_fault_archive(io.BytesIO(base), io.BytesIO(child), **args)


def test_every_blob_is_hashed_even_unreferenced_or_too_large_for_metadata():
    extra = ("sha256:" + "a" * 64, b"x" * (verifier.MAX_METADATA + 1))
    base, child, args = fixture(bad_blob=extra)
    with pytest.raises(IdentityError):
        verifier.verify_fault_archive(io.BytesIO(base), io.BytesIO(child), **args)


def test_gzip_expansion_budget_denies_before_large_materialization(monkeypatch):
    monkeypatch.setattr(verifier, "MAX_EXPANDED_LAYER", 100)
    base, child, args = fixture(compressed=True)
    with pytest.raises(IdentityError):
        verifier.verify_fault_archive(io.BytesIO(base), io.BytesIO(child), **args)


def test_wrong_declared_fault_blob_or_platform_denied():
    base, child, args = fixture()
    for change in ({"expected_fault_layer": "sha256:" + "f" * 64}, {"platform": "linux/arm64"}):
        with pytest.raises(IdentityError):
            verifier.verify_fault_archive(io.BytesIO(base), io.BytesIO(child), **(args | change))


@pytest.mark.parametrize("metadata", [{"mode": 0o777}, {"mode": 0o4755}, {"uid": 568}, {"gid": 568}])
def test_ancestor_permission_or_owner_change_denied(metadata):
    raw = tar([("app", b"", tarfile.DIRTYPE, metadata),
               ("app/dashboard/.wh.index.html", b"", tarfile.REGTYPE)])
    with pytest.raises(IdentityError):
        verifier._fault_layer(raw, RAW)


def test_whiteout_payload_or_missing_whiteout_denied():
    for entries in ([('app/dashboard/.wh.index.html', b'not empty', tarfile.REGTYPE)],
                    [('app/dashboard', b'', tarfile.DIRTYPE)]):
        with pytest.raises(IdentityError):
            verifier._fault_layer(tar(entries), RAW)


def test_raw_layer_budget_applies_without_gzip(monkeypatch):
    monkeypatch.setattr(verifier, "MAX_EXPANDED_LAYER", 100)
    base, child, args = fixture()
    with pytest.raises(IdentityError):
        verifier.verify_fault_archive(io.BytesIO(base), io.BytesIO(child), **args)


@pytest.mark.parametrize("change", [
    lambda m: m["layers"][-1].update(size=1),
    lambda m: m["layers"][-1].update(mediaType="application/vnd.oci.image.layer.v1.tar+zstd"),
])
def test_descriptor_size_or_unsupported_compression_denied(change):
    base, child, args = fixture(manifest_change=change)
    with pytest.raises(IdentityError):
        verifier.verify_fault_archive(io.BytesIO(base), io.BytesIO(child), **args)


@pytest.mark.parametrize("change", [lambda raw: raw[:-1], lambda raw: raw + b'trailing',
                                     lambda raw: raw + gzip.compress(b'other')])
def test_truncated_trailing_or_concatenated_gzip_denied(change):
    base, child, args = fixture(compressed=True, encoding_change=change)
    with pytest.raises(IdentityError):
        verifier.verify_fault_archive(io.BytesIO(base), io.BytesIO(child), **args)
