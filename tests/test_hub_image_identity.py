import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile

import pytest

spec = importlib.util.spec_from_file_location("hub_identity", Path(__file__).parents[1] / "scripts/inspect_hub_image_identity.py")
identity = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = identity
spec.loader.exec_module(identity)


def packed(value):
    return json.dumps(value, separators=(",", ":")).encode()


def sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def fixture(platform="amd64"):
    config = packed({"os": "linux", "architecture": platform, "config": {"Env": ["DO_NOT_DISCLOSE=fixture"]}})
    cfg = {"digest": sha(config), "size": len(config), "mediaType": "application/vnd.oci.image.config.v1+json"}
    manifest = packed({"schemaVersion": 2, "config": cfg, "layers": []})
    desc = {"digest": sha(manifest), "size": len(manifest), "mediaType": next(iter(identity.MANIFEST_TYPES)), "platform": {"os": "linux", "architecture": platform}}
    index = packed({"schemaVersion": 2, "manifests": [desc]})
    root = packed({"schemaVersion": 2, "manifests": [{"digest": sha(index), "size": len(index), "mediaType": next(iter(identity.INDEX_TYPES))}]})
    entries = {"index.json": root, **{"blobs/sha256/" + sha(v)[7:]: v for v in (config, manifest, index)}}
    return entries, sha(index), sha(manifest), sha(config)


def tar(entries):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, raw in entries:
            item = tarfile.TarInfo(name)
            item.size = len(raw)
            archive.addfile(item, io.BytesIO(raw))
    return data.getvalue()


def test_realistic_index_chain_and_config_not_runtime_identity():
    entries, index, manifest, config = fixture()
    parsed = identity.archive_metadata(io.BytesIO(tar(entries.items())))
    result = identity.inspect_metadata(parsed, index, "linux/amd64")
    assert result["config_digest"] == config
    assert result["platform_manifest_digest"] == manifest
    assert len({index, manifest, config}) == 3
    assert "DO_NOT_DISCLOSE" not in json.dumps(result)
    assert identity.inspect_metadata(parsed, manifest, "linux/amd64")["config_digest"] == config


@pytest.mark.parametrize("value", ["--help", "ubuntu:latest", "sha256:" + "A" * 64, "sha256:" + "0" * 63, "sha256:" + "0" * 64 + "\n", None])
def test_reject_nonimmutable_references(value):
    with pytest.raises(identity.IdentityError):
        identity.digest(value)


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'\xff', b'not-json'])
def test_strict_json(raw):
    with pytest.raises(identity.IdentityError):
        identity.strict_json(raw)


def test_duplicate_archive_entries():
    with pytest.raises(identity.IdentityError, match="Duplicate archive"):
        identity.archive_metadata(io.BytesIO(tar([("index.json", b"{}"), ("index.json", b"{}")])) )


@pytest.mark.parametrize("name", ["../secret", "/absolute", "a/../b", "a\\b"])
def test_paths_are_never_extracted_or_accepted(name):
    with pytest.raises(identity.IdentityError, match="Unsafe"):
        identity.archive_metadata(io.BytesIO(tar([(name, b"x")])) )


def test_layer_bytes_skipped_and_budgets(monkeypatch):
    monkeypatch.setattr(identity, "MAX_METADATA", 32)
    entries = identity.archive_metadata(io.BytesIO(tar([("layer.tar", b"x" * 200), ("manifest.json", b"[]")])))
    assert entries == {"manifest.json": b"[]"}
    with pytest.raises(identity.IdentityError, match="too large"):
        identity.archive_metadata(io.BytesIO(tar([("manifest.json", b"x" * 33)])))
    monkeypatch.setattr(identity, "MAX_ARCHIVE", 512)
    with pytest.raises(identity.IdentityError, match="byte limit"):
        identity.archive_metadata(io.BytesIO(tar([("a", b"x")])))


def test_metadata_cumulative_and_entry_limits(monkeypatch):
    monkeypatch.setattr(identity, "MAX_METADATA_TOTAL", 3)
    with pytest.raises(identity.IdentityError, match="budget"):
        identity.archive_metadata(io.BytesIO(tar([("a.json", b"1234")])))
    monkeypatch.setattr(identity, "MAX_ENTRIES", 1)
    with pytest.raises(identity.IdentityError, match="entry limit"):
        identity.archive_metadata(io.BytesIO(tar([("a", b"x"), ("b", b"x")])))


@pytest.mark.parametrize("mutation", ["missing", "changed", "wrong-size", "wrong-platform", "ambiguous"])
def test_reject_bad_chain(mutation):
    entries, index, manifest, config = fixture()
    if mutation == "missing":
        del entries["blobs/sha256/" + config[7:]]
    elif mutation == "changed":
        entries["blobs/sha256/" + config[7:]] += b" "
    elif mutation == "wrong-size":
        root = json.loads(entries["index.json"])
        root["manifests"][0]["size"] += 1
        entries["index.json"] = packed(root)
    elif mutation == "wrong-platform":
        with pytest.raises(identity.IdentityError):
            identity.inspect_metadata(entries, index, "linux/arm64")
        return
    else:
        root = json.loads(entries["index.json"])
        root["manifests"] *= 2
        entries["index.json"] = packed(root)
    with pytest.raises(identity.IdentityError):
        identity.inspect_metadata(entries, index, "linux/amd64")


def test_docker_compatibility_uses_config_identity_only():
    raw = packed({"os": "linux", "architecture": "amd64"})
    config = sha(raw)
    name = config[7:] + ".json"
    entries = {name: raw, "manifest.json": packed([{"Config": name, "Layers": []}])}
    assert identity.inspect_metadata(entries, config, "linux/amd64")["archive_format"] == "docker-compatible"
    with pytest.raises(identity.IdentityError):
        identity.inspect_metadata(entries, "sha256:" + "0" * 64, "linux/amd64")
    entries["manifest.json"] = packed([{"Config": name}, {"Config": name}])
    with pytest.raises(identity.IdentityError):
        identity.inspect_metadata(entries, config, "linux/amd64")


def test_process_is_fixed_read_only_and_reaped(monkeypatch):
    entries, image, _, _ = fixture()
    calls = []
    class Process:
        stdout = io.BytesIO(tar(entries.items()))
        def wait(self, timeout):
            calls.append(("wait", timeout))
            return 0
        def poll(self):
            return 0
        def kill(self):
            calls.append("kill")
    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return Process()
    monkeypatch.setattr(identity.subprocess, "Popen", popen)
    assert identity.inspect_local(image, "linux/amd64")["selection_reference"] == image
    command, kwargs = calls[0]
    assert command == ["docker", "image", "save", image]
    assert kwargs == {"stdout": identity.subprocess.PIPE, "stderr": identity.subprocess.DEVNULL, "stdin": identity.subprocess.DEVNULL}
    assert Process.stdout.closed
    assert ("wait", 5) in calls


def test_invalid_input_never_starts_process(monkeypatch):
    monkeypatch.setattr(identity.subprocess, "Popen", lambda *a, **k: pytest.fail("process started"))
    with pytest.raises(identity.IdentityError):
        identity.inspect_local("--output=secret", "linux/amd64")


def test_parse_failure_kills_and_reaps_producer(monkeypatch):
    events = []
    class Process:
        stdout = io.BytesIO(b"short")
        def poll(self): return None
        def kill(self): events.append("kill")
        def wait(self, timeout): events.append(("wait", timeout)); return -9
    monkeypatch.setattr(identity.subprocess, "Popen", lambda *a, **k: Process())
    with pytest.raises(identity.IdentityError):
        identity.inspect_local("sha256:" + "0" * 64, "linux/amd64")
    assert events == ["kill", ("wait", 5)]
    assert Process.stdout.closed


@pytest.mark.parametrize("change", ["platform-list", "media-list", "size-bool", "size-float", "schema-float", "scalar-descriptor", "scalar-root", "config-media"])
def test_malformed_oci_shapes_fail_closed(change):
    entries, index, manifest, _ = fixture()
    index_value = json.loads(entries["blobs/sha256/" + index[7:]])
    descriptor = index_value["manifests"][0]
    root = {"schemaVersion": 2, "manifests": [descriptor]}
    if change == "platform-list": descriptor["platform"] = ["linux"]
    elif change == "media-list": descriptor["mediaType"] = []
    elif change == "size-bool": descriptor["size"] = True
    elif change == "size-float": descriptor["size"] = float(descriptor["size"])
    elif change == "schema-float": root["schemaVersion"] = 2.0
    elif change == "scalar-descriptor": root["manifests"] = [42]
    elif change == "scalar-root": root = 42
    else:
        value = json.loads(entries["blobs/sha256/" + manifest[7:]])
        value["config"]["mediaType"] = ["bad"]
        raw = packed(value)
        manifest = sha(raw)
        descriptor["digest"], descriptor["size"] = manifest, len(raw)
        entries["blobs/sha256/" + manifest[7:]] = raw
    entries["index.json"] = packed(root)
    with pytest.raises(identity.IdentityError):
        identity.inspect_metadata(entries, manifest, "linux/amd64")


def test_deadline_kills_and_reaps_even_when_kill_races_exit(monkeypatch):
    events = []
    class Process:
        stdout = io.BytesIO(b"short")
        def poll(self): return 0
        def kill(self): events.append("kill"); raise ProcessLookupError()
        def wait(self, timeout): events.append(("wait", timeout)); return 0
    class Timer:
        def __init__(self, seconds, callback):
            assert seconds == identity.DEADLINE
            self.callback = callback
        def start(self): self.callback()
        def cancel(self): events.append("cancel")
    monkeypatch.setattr(identity.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(identity.threading, "Timer", Timer)
    with pytest.raises(identity.IdentityError):
        identity.inspect_local("sha256:" + "0" * 64, "linux/amd64")
    assert events == ["kill", "cancel", ("wait", 5)]
    assert Process.stdout.closed
