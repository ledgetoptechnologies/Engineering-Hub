"""Private Linux providers over actual files; no daemon, secrets or enrollment."""
import hashlib
import os
from pathlib import Path
import sys

import pytest

pytest.importorskip("fcntl", reason="Optional host package requires real Linux ownership")

from hub_container_executor.local_providers import (
    ImmutableTestSecretResolver, LocalProviderError, PrivateContentStore,
)


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "private-content"
    path.mkdir(mode=0o700)
    return path


def put(root, value):
    digest = "sha256:" + hashlib.sha256(value).hexdigest()
    path = root / digest[7:]
    path.write_bytes(value)
    path.chmod(0o600)
    return digest, path


def store(root, maximum_bytes=1024):
    return PrivateContentStore(root, maximum_bytes=maximum_bytes, allow_ephemeral_test_storage=True)


def secrets(root, bindings):
    return ImmutableTestSecretResolver(root, bindings=bindings, allow_ephemeral_test_storage=True)


def test_read_only_digest_store_does_not_provision_or_modify_files(root):
    digest, path = put(root, b"test content")
    before = path.stat()
    with store(root) as content:
        assert content.get(digest) == b"test content"
        assert content.get(digest) == b"test content"
    assert set(root.iterdir()) == {path}
    after = path.stat()
    assert (before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns)
    with pytest.raises(LocalProviderError):
        content.get(digest)
    content.close()
    with store(root) as reopened:
        assert reopened.get(digest) == b"test content"


@pytest.mark.parametrize("value", [None, "", "sha256:" + "A" * 64, "a" * 64, "sha256:../escape",
                                 "sha256:" + "a" * 64 + "/other", "sha256:" + "a" * 65, 123])
def test_only_exact_digest_lookup_admitted_before_file_open(root, monkeypatch, value):
    with store(root) as content:
        monkeypatch.setattr(os, "open", lambda *a, **kw: pytest.fail("Invalid lookup must not open files"))
        with pytest.raises(LocalProviderError):
            content.get(value)


@pytest.mark.parametrize("maximum", [None, 0, -1, True, 1024 * 1024 + 1, "1024"])
def test_explicit_byte_limits_validated(root, maximum):
    with pytest.raises(LocalProviderError):
        store(root, maximum)


def test_missing_empty_oversized_and_hash_changed_files_are_denied(root):
    small, small_path = put(root, b"good")
    big, _ = put(root, b"too large")
    empty, _ = put(root, b"")
    with store(root, maximum_bytes=4) as content:
        assert content.get(small) == b"good"
        for digest in ("sha256:" + "f" * 64, big, empty):
            with pytest.raises(LocalProviderError):
                content.get(digest)
        small_path.write_bytes(b"evil")
        with pytest.raises(LocalProviderError, match="digest mismatch") as error:
            content.get(small)
        assert str(small_path) not in str(error.value) and small not in str(error.value)


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "public", "directory", "fifo"])
def test_nonsingle_private_regular_content_files_denied(root, attack):
    digest, path = put(root, b"fixture")
    if attack == "symlink":
        target = root / "target"
        path.rename(target)
        path.symlink_to(target)
    elif attack == "hardlink":
        os.link(path, root / "extra-link")
    elif attack == "public":
        path.chmod(0o644)
    elif attack == "directory":
        path.unlink()
        path.mkdir(mode=0o700)
    else:
        path.unlink()
        os.mkfifo(path, 0o600)
    with store(root) as content:
        with pytest.raises(LocalProviderError):
            content.get(digest)


@pytest.mark.parametrize("attack", ["missing", "relative", "symlink", "permissions", "wrong-owner"])
def test_root_admission_is_private_existing_and_explicit(root, attack):
    path = root
    if attack == "missing":
        path = root / "missing"
    elif attack == "relative":
        path = Path("relative-root")
    elif attack == "symlink":
        alias = root.with_name("alias")
        alias.symlink_to(root, target_is_directory=True)
        path = alias
    elif attack == "permissions":
        root.chmod(0o755)
    else:
        if os.geteuid() != 0:
            pytest.skip("Owner change requires isolated test root")
        os.chown(root, 12345, -1)
    with pytest.raises(LocalProviderError):
        store(path)


def test_nonlinux_rejected_before_filesystem_access(root, monkeypatch):
    monkeypatch.setattr("hub_container_executor.local_providers.sys.platform", "win32")
    monkeypatch.setattr(os, "open", lambda *a, **kw: pytest.fail("Non-Linux must not open files"))
    with pytest.raises(LocalProviderError):
        store(root)


def test_pinned_root_replacement_denied(root):
    digest, _ = put(root, b"fixture")
    with store(root) as content:
        root.rename(root.with_name("old-root"))
        root.mkdir(mode=0o700)
        put(root, b"fixture")
        with pytest.raises(LocalProviderError):
            content.get(digest)


def test_file_owner_mismatch_denied(root):
    if os.geteuid() != 0:
        pytest.skip("Owner change requires isolated test root")
    digest, path = put(root, b"fixture")
    os.chown(path, 12345, -1)
    with store(root) as content:
        with pytest.raises(LocalProviderError):
            content.get(digest)


def test_deleted_root_error_is_sanitized_even_on_context_entry(root):
    content = store(root)
    root.rmdir()
    try:
        with pytest.raises(LocalProviderError) as error:
            content.__enter__()
        assert str(root) not in str(error.value)
    finally:
        content.close()


def test_mutation_during_bounded_read_detected_and_fd_closed(root, monkeypatch):
    digest, path = put(root, b"a" * 100000)
    original_read = os.read
    original_close = os.close
    reads, closes = [], []

    def changed(fd, size):
        reads.append((fd, size))
        value = original_read(fd, size)
        if len(reads) == 1:
            replacement = root / "replacement"
            replacement.write_bytes(b"a" * 100000)
            replacement.chmod(0o600)
            replacement.replace(path)
        return value

    def closed(fd):
        closes.append(fd)
        original_close(fd)

    with store(root, maximum_bytes=100000) as content:
        monkeypatch.setattr(os, "read", changed)
        monkeypatch.setattr(os, "close", closed)
        with pytest.raises(LocalProviderError):
            content.get(digest)
        assert reads and all(size <= 65536 for _, size in reads)
        assert reads[0][0] in closes


def test_versioned_secret_binding_exact_immutable_without_latest_fallback(root):
    first, first_path = put(root, b"synthetic-version-one")
    second, _ = put(root, b"synthetic-version-two")
    bindings = (("test-codex", 1, first), ("test-codex", 2, second))
    with secrets(root, bindings) as resolver:
        assert resolver.resolve_test_secret("test-codex", 1) == "synthetic-version-one"
        assert resolver.resolve_test_secret("test-codex", 2) == "synthetic-version-two"
        for slot, version in (("test-codex", 3), ("test-other", 1)):
            with pytest.raises(LocalProviderError):
                resolver.resolve_test_secret(slot, version)
        with pytest.raises(TypeError):
            resolver._bindings["test-codex", 1] = second
        first_path.write_bytes(b"changed-secret-value")
        with pytest.raises(LocalProviderError) as error:
            resolver.resolve_test_secret("test-codex", 1)
        assert "changed-secret-value" not in str(error.value) and str(root) not in str(error.value)
    with pytest.raises(LocalProviderError):
        resolver.resolve_test_secret("test-codex", 2)


@pytest.mark.parametrize("slot,version", [("production", 1), ("test-", 1), ("test-X", 1), ("test-../escape", 1),
                                         ("test-ok", 0), ("test-ok", True), ("test-ok", "1"), ("test-ok", 2**63)])
def test_invalid_secret_binding_lookup_denied(root, slot, version):
    digest, _ = put(root, b"synthetic")
    with pytest.raises(LocalProviderError):
        secrets(root, ((slot, version, digest),))
    with secrets(root, (("test-valid", 1, digest),)) as resolver:
        with pytest.raises(LocalProviderError):
            resolver.resolve_test_secret(slot, version)


@pytest.mark.parametrize("value", [b"", b"\xff", b"line\nbreak", b"null\x00byte", b"delete\x7f", b"a" * 4097])
def test_invalid_secret_bytes_do_not_escape_in_errors(root, value):
    digest, _ = put(root, value)
    with secrets(root, (("test-slot", 1, digest),)) as resolver:
        with pytest.raises(LocalProviderError) as error:
            resolver.resolve_test_secret("test-slot", 1)
        assert str(error.value) == "Versioned test-secret unavailable"


def test_duplicate_bindings_and_bad_configuration_rejected_without_provisioning(root):
    digest, _ = put(root, b"synthetic")
    binding = ("test-slot", 1, digest)
    for values in ([], (), (binding, binding), (["test-slot", 1, digest],), (("test-slot", 1, "bad"),)):
        with pytest.raises(LocalProviderError):
            secrets(root, values)


def test_content_store_bytes_never_substitute_for_trusted_build_verifier(root):
    from hub_container_executor.build_record import BuildRecordError, verify_image_record
    from tests.test_hub_container_executor_build_record import FakeTrust, make_record
    manifest, record, blobs = make_record()
    for digest, raw in blobs.items():
        assert put(root, raw)[0] == digest
    record_digest, _ = put(root, record)
    assert record_digest == manifest.candidate.provenance.attestation_digest
    with store(root, maximum_bytes=65536) as records, store(root, maximum_bytes=1048576) as metadata:
        with pytest.raises(BuildRecordError, match="trusted build verifier"):
            verify_image_record(manifest, "candidate", records, metadata, None)
        # Explicit test-only trust, never a production provider or default.
        trust = FakeTrust()
        verified = verify_image_record(manifest, "candidate", records, metadata, trust)
        assert verified.source_commit == manifest.candidate.source_commit and trust.calls


def test_forked_child_denies_get_and_closes_without_inherited_mutex_or_parent_damage(root):
    import multiprocessing
    import threading

    digest, _ = put(root, b"synthetic-parent-content")
    content = store(root)
    held, release = threading.Event(), threading.Event()

    def hold_mutex():
        with content._mutex:
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold_mutex, daemon=True)
    holder.start()
    assert held.wait(2), "Parent mutex was not held before fork"
    context = multiprocessing.get_context("fork")
    receive, send = context.Pipe(duplex=False)

    def child():
        receive.close()
        try:
            try:
                content.get(digest)
            except LocalProviderError:
                pass
            else:
                raise AssertionError("Child inherited usable content authority")
            # The parent's other thread still owns the inherited mutex: both
            # get denial and descriptor cleanup must occur without taking it.
            content.close()
            content.close()
            send.send("denied-and-closed")
        finally:
            send.close()

    process = context.Process(target=child)
    try:
        process.start()
        send.close()
        assert receive.poll(3), "Child get/close blocked on inherited mutex"
        assert receive.recv() == "denied-and-closed"
        process.join(3)
        assert process.exitcode == 0
        release.set()
        holder.join(2)
        assert not holder.is_alive()
        assert content.get(digest) == b"synthetic-parent-content"
    finally:
        release.set()
        holder.join(2)
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
                process.join(2)
            if process.is_alive():
                process.kill()
                process.join(2)
            process.close()
        receive.close()
        send.close()
        content.close()
