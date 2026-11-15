"""Opt-in key custody tests; no daemon, transport or fencing claimed."""
import hashlib
import hmac
import os
from pathlib import Path
import sys

import pytest

pytest.importorskip("fcntl")
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux private local filesystem required")

from hub_container_executor.effect_mac_key import EffectMacKey, EffectMacKeyError, create_for_new_journal


def directory(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    return private


def test_new_key_only_and_domain_separated_reopen(tmp_path):
    root = directory(tmp_path)
    key_id = create_for_new_journal(root, "ops.sqlite")
    key = root / "ops.sqlite.effect-binding.key"
    assert key.stat().st_mode & 0o777 == 0o600 and len(key.read_bytes()) == 32
    with pytest.raises(EffectMacKeyError):
        create_for_new_journal(root, "ops.sqlite")
    journal = root / "ops.sqlite"
    journal.touch(mode=0o600)
    holder = EffectMacKey(root, "ops.sqlite", expected_key_id=key_id)
    try:
        expected = hmac.new(key.read_bytes(), b"engineering-hub.effect-binding.v1\0" + b"binding", hashlib.sha256).hexdigest()
        assert holder.mac(b"binding") == "hmac-sha256:" + expected
        assert holder.mac(b"other") != holder.mac(b"binding")
    finally:
        holder.close()
    reopened = EffectMacKey(root, "ops.sqlite", expected_key_id=key_id)
    try:
        assert reopened.mac(b"binding") == "hmac-sha256:" + expected
    finally:
        reopened.close()


def test_existing_journal_missing_or_damaged_key_never_regenerates(tmp_path):
    root = directory(tmp_path)
    (root / "ops.sqlite").touch(mode=0o600)
    with pytest.raises(EffectMacKeyError):
        create_for_new_journal(root, "ops.sqlite")
    with pytest.raises(EffectMacKeyError):
        EffectMacKey(root, "ops.sqlite", expected_key_id="sha256:" + "0" * 64)
    assert not (root / "ops.sqlite.effect-binding.key").exists()
    key = root / "ops.sqlite.effect-binding.key"
    key.write_bytes(b"incomplete")
    key.chmod(0o600)
    with pytest.raises(EffectMacKeyError):
        EffectMacKey(root, "ops.sqlite", expected_key_id="sha256:" + "0" * 64)
    assert key.read_bytes() == b"incomplete"


def test_offline_key_replacement_fails_against_prior_id(tmp_path):
    root = directory(tmp_path)
    key_id = create_for_new_journal(root, "ops.sqlite")
    (root / "ops.sqlite").touch(mode=0o600)
    key = root / "ops.sqlite.effect-binding.key"
    key.write_bytes(b"z" * 32)
    with pytest.raises(EffectMacKeyError):
        EffectMacKey(root, "ops.sqlite", expected_key_id=key_id)


def test_fsync_failure_retains_partial_and_denies_regeneration(tmp_path, monkeypatch):
    root = directory(tmp_path)
    def fail_fsync(_fd):
        raise OSError("injected fsync failure")
    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(EffectMacKeyError):
        create_for_new_journal(root, "ops.sqlite")
    key = root / "ops.sqlite.effect-binding.key"
    assert key.exists()
    with pytest.raises(EffectMacKeyError):
        create_for_new_journal(root, "ops.sqlite")


def test_fork_cannot_use_parent_key_capability(tmp_path):
    if not hasattr(os, "fork"):
        pytest.skip("Linux fork unavailable")
    root = directory(tmp_path)
    key_id = create_for_new_journal(root, "ops.sqlite")
    (root / "ops.sqlite").touch(mode=0o600)
    holder = EffectMacKey(root, "ops.sqlite", expected_key_id=key_id)
    pid = os.fork()
    if pid == 0:
        try:
            holder.mac(b"binding")
        except EffectMacKeyError:
            os._exit(0)
        os._exit(1)
    try:
        assert os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == 0
        assert holder.mac(b"binding").startswith("hmac-sha256:")
    finally:
        holder.close()


@pytest.mark.parametrize("attack", ["replace", "hardlink", "mode", "content", "journal_replace", "key_symlink"])
def test_identity_and_material_changes_fail_closed(tmp_path, attack):
    root = directory(tmp_path)
    key_id = create_for_new_journal(root, "ops.sqlite")
    journal = root / "ops.sqlite"
    journal.touch(mode=0o600)
    key = root / "ops.sqlite.effect-binding.key"
    holder = EffectMacKey(root, "ops.sqlite", expected_key_id=key_id)
    try:
        if attack == "replace":
            key.rename(root / "old-key")
            key.write_bytes(b"x" * 32); key.chmod(0o600)
        elif attack == "hardlink":
            os.link(key, root / "extra")
        elif attack == "mode":
            key.chmod(0o644)
        elif attack == "content":
            key.write_bytes(b"x" * 32)
        elif attack == "journal_replace":
            journal.rename(root / "old-journal")
            journal.touch(mode=0o600)
        else:
            key.rename(root / "old-key")
            key.symlink_to(root / "old-key")
        with pytest.raises(EffectMacKeyError):
            holder.mac(b"binding")
    finally:
        holder.close()
