"""Pure loader checks: no listeners, credentials, state initialization or migration."""
from dataclasses import FrozenInstanceError
import json
import os

import pytest

from engineering_hub.deployment_config import load_deployment_config


def valid():
    return {"schema_version": 1, "base_url": "https://hub.example.com",
            "cloudflare_access": {"issuer": "https://hub-test.cloudflareaccess.com",
                                  "audience": "hub-only", "allowed_emails": ["beau@example.com"]},
            "agent_allowed_hosts": ["192.168.50.80:8770", "localhost:8770"],
            "agent_allowed_ips": ["192.168.60.92", "::1"]}


def write(tmp_path, document):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_valid_typed_frozen_derived_paths_and_no_state_or_environment_mutation(tmp_path):
    path = write(tmp_path, valid())
    before_env = dict(os.environ)
    before = path.read_bytes()
    result = load_deployment_config(path)
    assert result.state_root == tmp_path
    assert result.hub_database == tmp_path / "hub.sqlite3"
    assert result.bus_paths == {
        "AGENT_BUS_DB": tmp_path / "agent-message-bus/messages.sqlite3",
        "AGENT_BUS_AUDIT": tmp_path / "agent-message-bus/audit.jsonl",
        "AGENT_BUS_FILE_ROOT": tmp_path / "agent-message-bus/files"}
    assert result.auth_config.human_auth_mode == "cloudflare_access"
    assert result.auth_config.cloudflare_config.audience == "hub-only"
    assert result.allowed_hosts == ("192.168.50.80:8770", "localhost:8770")
    assert result.allowed_ips == frozenset({"192.168.60.92", "::1"})
    with pytest.raises(FrozenInstanceError):
        result.state_root = tmp_path / "elsewhere"
    result.bus_paths.clear()
    assert len(result.bus_paths) == 3
    assert dict(os.environ) == before_env
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("key", list(valid()))
def test_missing_top_level_key(tmp_path, key):
    document = valid()
    document.pop(key)
    with pytest.raises(ValueError, match="^Invalid Hub deployment configuration$"):
        load_deployment_config(write(tmp_path, document))


@pytest.mark.parametrize("key", ["issuer", "audience", "allowed_emails"])
def test_missing_access_key(tmp_path, key):
    document = valid()
    document["cloudflare_access"].pop(key)
    with pytest.raises(ValueError, match="^Invalid Hub deployment configuration$"):
        load_deployment_config(write(tmp_path, document))


@pytest.mark.parametrize("target,key,value", [
    (None, "schema_version", True), (None, "schema_version", 2),
    (None, "schema_version", 1.0), (None, "schema_version", "1"),
    (None, "base_url", None), (None, "base_url", 42),
    (None, "base_url", "http://hub.example.com"),
    (None, "base_url", "https://user:secret@hub.example.com"),
    (None, "base_url", "https://hub.example.com/path"),
    (None, "cloudflare_access", []), (None, "cloudflare_access", None),
    ("cloudflare_access", "issuer", "https://evil.example.com"),
    ("cloudflare_access", "issuer", ""), ("cloudflare_access", "issuer", []),
    ("cloudflare_access", "audience", ""), ("cloudflare_access", "audience", True),
    ("cloudflare_access", "audience", "a b"),
    ("cloudflare_access", "allowed_emails", []),
    ("cloudflare_access", "allowed_emails", "beau@example.com"),
    ("cloudflare_access", "allowed_emails", [None]),
    ("cloudflare_access", "allowed_emails", ["*@example.com"]),
    ("cloudflare_access", "allowed_emails", ["beau@example.com", "BEAU@example.com"]),
    (None, "agent_allowed_hosts", []), (None, "agent_allowed_hosts", "localhost"),
    (None, "agent_allowed_hosts", ["localhost", "localhost"]),
    (None, "agent_allowed_hosts", ["*"]), (None, "agent_allowed_hosts", [None]),
    (None, "agent_allowed_hosts", ["host:0"]),
    (None, "agent_allowed_hosts", ["https://host"]),
    (None, "agent_allowed_ips", []), (None, "agent_allowed_ips", "127.0.0.1"),
    (None, "agent_allowed_ips", ["host"]), (None, "agent_allowed_ips", [None]),
    (None, "agent_allowed_ips", ["127.0.0.1/8"]),
    (None, "agent_allowed_ips", ["fe80::1%eth0"]),
    (None, "agent_allowed_ips", ["127.0.0.1", "127.0.0.1"]),
    (None, "agent_allowed_ips", ["2001:0DB8::1"]),
])
def test_invalid_value(tmp_path, target, key, value):
    document = valid()
    (document if target is None else document[target])[key] = value
    with pytest.raises(ValueError, match="^Invalid Hub deployment configuration$"):
        load_deployment_config(write(tmp_path, document))


@pytest.mark.parametrize("target,key", [(None, "CODEX_TOKEN"), (None, "database"),
    (None, "work_queue_enabled"), ("cloudflare_access", "secret")])
def test_unknown_keys_do_not_echo_contents(tmp_path, target, key):
    document = valid()
    (document if target is None else document[target])[key] = "SECRET-SHOULD-NOT-LEAK"
    with pytest.raises(ValueError) as caught:
        load_deployment_config(write(tmp_path, document))
    assert str(caught.value) == "Invalid Hub deployment configuration"
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("raw", [b"", b"{", b"[]", b"null", b"42", b"\xff", b"\xef\xbb\xbf{}",
    b'{"schema_version":1,"schema_version":1}', b'{"a":{"issuer":"one","issuer":"two"}}',
    b'{"bad":NaN}', b'{"bad":Infinity}', b'{"bad":-Infinity}', b" " * (65536 + 1),
    b"[" * 2000 + b"]" * 2000], ids=["empty", "truncated", "array", "null", "number",
        "invalid-utf8", "bom", "duplicate-root", "duplicate-nested", "nan", "infinity",
        "negative-infinity", "oversize", "excessive-nesting"])
def test_invalid_file_formats_are_generic(tmp_path, raw):
    path = tmp_path / "settings.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="^Invalid Hub deployment configuration$"):
        load_deployment_config(path)


@pytest.mark.parametrize("path", ["relative/settings.json", "settings.json", "", None, 5])
def test_absolute_explicit_path_required(path):
    with pytest.raises(ValueError, match="^Invalid Hub deployment configuration$"):
        load_deployment_config(path)


def test_missing_file_and_directory_are_generic(tmp_path):
    for path in (tmp_path / "missing.json", tmp_path):
        with pytest.raises(ValueError, match="^Invalid Hub deployment configuration$"):
            load_deployment_config(path)


def test_exact_size_limit_accepts_valid_document_but_one_extra_byte_is_rejected(tmp_path):
    path = write(tmp_path, valid())
    raw = path.read_bytes()
    path.write_bytes(raw + b" " * (65536 - len(raw)))
    assert load_deployment_config(path).auth_config.origin == "https://hub.example.com"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="^Invalid Hub deployment configuration$"):
        load_deployment_config(path)
