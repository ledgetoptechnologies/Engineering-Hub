"""Private Linux UDS fixtures; synthetic keys, not installed issuer acceptance."""
import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import select
import signal
import socket
import struct
import sys
import tempfile
import threading
import time
import uuid

import pytest

if sys.platform != "linux":
    pytest.skip("Direct launch transport requires Linux peer credentials", allow_module_level=True)

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from engineering_hub.container_pilot_manifest import Platform
from hub_container_executor.launch_transport import REQUEST_PROTOCOL, UnixLaunchReceiptTransport
import hub_container_executor.launch_transport as launch_transport
from hub_container_executor.runtime_identity import (
    ExecutorReleaseEnrollment, LaunchReceiptIdentityVerifier, RuntimeIdentityError,
    SIGNATURE_DOMAIN,
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def frame(raw):
    return struct.pack("!I", len(raw)) + raw


def read_exact(connection, size):
    result = b""
    while len(result) < size:
        value = connection.recv(size - len(result))
        if not value:
            raise EOFError("Fixture peer closed")
        result += value
    return result


def request(connection, state):
    size = struct.unpack("!I", read_exact(connection, 4))[0]
    assert 0 < size <= 256
    raw = read_exact(connection, size)
    value = json.loads(raw)
    assert raw == canonical(value)
    assert set(value) == {"nonce", "protocol"}  # No caller-selected PID/container/key.
    assert value["protocol"] == REQUEST_PROTOCOL
    assert connection.recv(1) == b""  # Explicit one-frame request half-close.
    state["request"] = value
    state["peer"] = struct.unpack("=iII", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    return value


@contextmanager
def issuer(handler):
    # Never touch real host issuer paths, enrollment, credentials or services.
    with tempfile.TemporaryDirectory(prefix="eh-uds-", dir="/tmp") as directory:
        path = str(Path(directory) / "issuer.sock")
        state, errors = {}, []
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(path)
            listener.listen(1)
            listener.settimeout(1)
            state["listener"] = listener
            state["accept_count"] = 0

            def serve():
                try:
                    connection, _ = listener.accept()
                    state["accept_count"] += 1
                    with connection:
                        connection.settimeout(1)
                        handler(connection, state)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Expected when a deadline/framing denial closes the FD.
                except Exception as error:
                    errors.append(error)

            worker = threading.Thread(target=serve)
            worker.start()
            try:
                yield path, state
            finally:
                worker.join(2)
                assert not worker.is_alive(), "Bounded fixture thread did not terminate"
                assert not errors, "Synthetic issuer fixture failed"


def transport(path, **changes):
    values = dict(socket_path=path, expected_issuer_uid=os.geteuid(), expected_issuer_gid=os.getegid())
    values.update(changes)
    return UnixLaunchReceiptTransport(**values)


RAW = canonical({"payload": {}, "signature": "synthetic-unverified-envelope"})
NONCE = "a" * 64


def test_real_peer_direct_process_fragmented_io_and_owned_fd_closed(monkeypatch):
    def handler(connection, state):
        request(connection, state)
        for byte in frame(RAW):
            connection.sendall(bytes([byte]))
        state["eof"] = connection.recv(1)

    with issuer(handler) as (path, state):
        original_socket, client_sockets = socket.socket, []
        def capture_client_socket(*args, **kwargs):
            connection = original_socket(*args, **kwargs)
            # accept() wraps its existing server FD with fileno=...; the
            # listener was constructed before patching. Retain only the new FD
            # created by the actual client transport, not either server socket.
            if kwargs.get("fileno") is None and len(args) < 4:
                client_sockets.append(connection)
            return connection
        monkeypatch.setattr(launch_transport.socket, "socket", capture_client_socket)
        client = transport(path)
        assert client.issue_receipt(nonce=NONCE, timeout_ms=1000) == RAW
        assert len(client_sockets) == 1
        assert client_sockets[0].fileno() == -1  # EOF alone only proves SHUT_WR.
        with pytest.raises(RuntimeIdentityError):
            client.issue_receipt(nonce=NONCE, timeout_ms=1000)
        client.close()
    assert state["request"]["nonce"] == NONCE
    assert state["peer"] == (os.getpid(), os.geteuid(), os.getegid())
    assert state["eof"] == b""


@pytest.mark.parametrize("field", ["expected_issuer_uid", "expected_issuer_gid"])
def test_wrong_issuer_credentials_rejected_before_sending_nonce_and_fd_closed(field):
    def handler(connection, state):
        state["received"] = connection.recv(1)

    value = (os.geteuid() if field.endswith("uid") else os.getegid()) + 1
    with issuer(handler) as (path, state):
        with pytest.raises(RuntimeIdentityError, match="^Direct launch issuer exchange unavailable$"):
            transport(path, **{field: value}).issue_receipt(nonce=NONCE, timeout_ms=1000)
    assert state["received"] == b""


@pytest.mark.parametrize("wire", [
    struct.pack("!I", 8193), struct.pack("!I", 0), b"\x00\x00",
    struct.pack("!I", len(RAW)) + RAW[:-1],
    frame(b'{"payload":{},"signature":"x","signature":"y"}'),
    frame(b'{"payload":{"x":1.0},"signature":"x"}'),
    frame(b'{"payload":{"x":NaN},"signature":"x"}'),
    frame(b'{"payload":{}, "signature":"x"}'),
    frame(b'{"payload":[],"signature":"x"}'),
    frame(b'{"payload":{},"signature":"x","extra":1}'),
    frame(b"\xff"),
])
def test_strict_bounded_response_framing_and_canonical_syntax(wire):
    def handler(connection, state):
        request(connection, state)
        connection.sendall(wire)

    with issuer(handler) as (path, _):
        with pytest.raises(RuntimeIdentityError):
            transport(path).issue_receipt(nonce=NONCE, timeout_ms=1000)


@pytest.mark.parametrize("trailing", [b"garbage", frame(RAW)])
def test_rejects_trailing_garbage_or_second_response_frame(trailing):
    def handler(connection, state):
        request(connection, state)
        connection.sendall(frame(RAW) + trailing)

    with issuer(handler) as (path, _):
        with pytest.raises(RuntimeIdentityError):
            transport(path).issue_receipt(nonce=NONCE, timeout_ms=1000)


def test_response_without_issuer_eof_uses_original_deadline():
    def handler(connection, state):
        request(connection, state)
        connection.sendall(frame(RAW))
        time.sleep(.25)  # An otherwise valid frame cannot renew the EOF budget.

    with issuer(handler) as (path, _):
        with pytest.raises(RuntimeIdentityError):
            transport(path).issue_receipt(nonce=NONCE, timeout_ms=100)


def test_whole_deadline_not_renewed_by_slow_response_fragments():
    def handler(connection, state):
        request(connection, state)
        for byte in frame(RAW):
            connection.sendall(bytes([byte]))
            time.sleep(.04)

    with issuer(handler) as (path, _):
        client = transport(path)
        started = time.monotonic()
        with pytest.raises(RuntimeIdentityError):
            client.issue_receipt(nonce=NONCE, timeout_ms=100)
        assert time.monotonic() - started < .5
        with pytest.raises(RuntimeIdentityError):
            client.issue_receipt(nonce=NONCE, timeout_ms=1000)


def test_zero_peer_pid_is_not_mistaken_for_an_authentication_failure(monkeypatch):
    # Synthetic ABI-view override over a real UDS exchange; this is not an actual
    # nested PID-namespace acceptance test. UID/GID still come from the kernel.
    class OutsidePidNamespace:
        size = 12
        @staticmethod
        def unpack(raw):
            _, uid, gid = struct.unpack("=iII", raw)
            return 0, uid, gid

    monkeypatch.setattr(launch_transport, "_CREDENTIALS", OutsidePidNamespace())
    def handler(connection, state):
        request(connection, state)
        connection.sendall(frame(RAW))

    with issuer(handler) as (path, _):
        assert transport(path).issue_receipt(nonce=NONCE, timeout_ms=1000) == RAW


def test_concurrent_duplicate_and_close_do_not_wait_for_admitted_exchange():
    received, release, duplicate_done, closed = (threading.Event() for _ in range(4))
    results, errors = [], []
    def handler(connection, state):
        request(connection, state)
        received.set()
        assert release.wait(2), "Fixture release was not signalled"
        connection.sendall(frame(RAW))
        state["eof"] = connection.recv(1)
        state["listener"].settimeout(.05)
        try:
            extra, _ = state["listener"].accept()
        except socket.timeout:
            return
        with extra:
            state["accept_count"] += 1

    with issuer(handler) as (path, state):
        client = transport(path)
        def first():
            try:
                results.append(client.issue_receipt(nonce=NONCE, timeout_ms=2000))
            except Exception as error:
                errors.append(error)
        def duplicate():
            try:
                client.issue_receipt(nonce=NONCE, timeout_ms=100)
                errors.append(AssertionError("Duplicate unexpectedly admitted"))
            except RuntimeIdentityError:
                duplicate_done.set()
        def closer():
            client.close()
            closed.set()
        worker = threading.Thread(target=first)
        second = threading.Thread(target=duplicate)
        closer_thread = threading.Thread(target=closer)
        worker.start()
        try:
            assert received.wait(.5)
            second.start()
            closer_thread.start()
            assert duplicate_done.wait(.5), "Duplicate waited behind the first exchange"
            assert closed.wait(.5), "close() waited behind the first exchange"
            assert worker.is_alive()  # Revocation does not cancel admitted work.
        finally:
            release.set()
            for thread in (worker, second, closer_thread):
                if thread.ident is not None:
                    thread.join(2)
                    assert not thread.is_alive()
    assert not errors
    assert results == [RAW] and state["eof"] == b"" and state["accept_count"] == 1


@pytest.mark.parametrize("path", ["relative.sock", "@abstract", "/tmp/../tmp/a", "/tmp//a", "/tmp/a/", "/tmp/a\x00b", "//tmp/a"])
def test_requires_canonical_explicit_absolute_nonabstract_path(path):
    with pytest.raises(RuntimeIdentityError):
        transport(path)


def test_rejects_symlink_leaf_and_regular_file_without_connection():
    with tempfile.TemporaryDirectory(prefix="eh-uds-", dir="/tmp") as directory:
        leaf = Path(directory) / "actual.sock"
        link = Path(directory) / "link.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(leaf))
            listener.listen(1)
            link.symlink_to(leaf)
            with pytest.raises(RuntimeIdentityError):
                transport(str(link)).issue_receipt(nonce=NONCE, timeout_ms=1000)
        plain = Path(directory) / "regular"
        plain.write_bytes(b"fixture")
        with pytest.raises(RuntimeIdentityError):
            transport(str(plain)).issue_receipt(nonce=NONCE, timeout_ms=1000)


def test_leaf_replacement_during_exchange_rejected():
    holder = {}
    def handler(connection, state):
        request(connection, state)
        Path(holder["path"]).unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as replacement:
            replacement.bind(holder["path"])
            connection.sendall(frame(RAW))
            state["eof"] = connection.recv(1)

    with issuer(handler) as (path, _):
        holder["path"] = path
        with pytest.raises(RuntimeIdentityError):
            transport(path).issue_receipt(nonce=NONCE, timeout_ms=1000)


def test_inherited_transport_denied_before_inherited_lock_and_close_revokes():
    # No actual issuer is necessary: the child must fail before opening any FD.
    client = transport("/tmp/fixture-issuer-never-opened.sock")
    read_fd, write_fd = os.pipe()
    client._lock.acquire()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            client.issue_receipt(nonce=NONCE, timeout_ms=1000)
        except RuntimeIdentityError:
            os.write(write_fd, b"denied")
        finally:
            os.close(write_fd)
            os._exit(0)
    os.close(write_fd)
    try:
        assert select.select([read_fd], [], [], 1)[0]
        assert os.read(read_fd, 16) == b"denied"
    finally:
        client._lock.release()
        os.close(read_fd)
        if os.waitpid(child, os.WNOHANG)[0] == 0:
            os.kill(child, signal.SIGKILL)  # Only this exact synthetic child.
            os.waitpid(child, 0)
    client.close()
    with pytest.raises(RuntimeIdentityError):
        client.issue_receipt(nonce=NONCE, timeout_ms=1000)


@pytest.mark.parametrize("nonce,timeout", [("A" * 64, 1000), ("a" * 63, 1000),
                                          (NONCE, True), (NONCE, 99), (NONCE, 10001)])
def test_invalid_challenge_or_timeout_rejected_before_opening_socket(nonce, timeout):
    with pytest.raises(RuntimeIdentityError):
        transport("/tmp/fixture-issuer-never-opened.sock").issue_receipt(nonce=nonce, timeout_ms=timeout)


def test_direct_real_socket_composes_with_existing_signed_identity_verifier():
    # Ephemeral fixture key and synthetic claims are NOT a real launch enrollment.
    key = Ed25519PrivateKey.generate()
    enrollment = ExecutorReleaseEnrollment(
        issuer_id="synthetic-uds-issuer",
        public_key=key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
        implementation_commit="c" * 40,
        runnable_digest="sha256:" + "1" * 64,
        platform_manifest_digest="sha256:" + "2" * 64,
        image_config_id="sha256:" + "3" * 64,
        platform=Platform(os="linux", architecture="amd64", variant=None),
        launch_profile_digest="sha256:" + "4" * 64,
    )
    def handler(connection, state):
        challenge = request(connection, state)
        now = int(time.time() * 1000)
        payload = dict(protocol="engineering-hub.executor-launch.v1",
                       audience="engineering-hub.container-executor", nonce=challenge["nonce"],
                       receipt_id=str(uuid.uuid4()), launch_id=str(uuid.uuid4()), boot_id=str(uuid.uuid4()),
                       issuer_id=enrollment.issuer_id, authenticated_host_pid=os.getpid(),
                       process_start_ticks=123, container_id="a" * 64,
                       runnable_digest=enrollment.runnable_digest,
                       platform_manifest_digest=enrollment.platform_manifest_digest,
                       image_config_id=enrollment.image_config_id,
                       platform=enrollment.platform.model_dump(mode="json"),
                       implementation_commit=enrollment.implementation_commit,
                       launch_profile_digest=enrollment.launch_profile_digest,
                       issued_at_ms=now, expires_at_ms=now + 10000)
        signature = base64.urlsafe_b64encode(key.sign(SIGNATURE_DOMAIN + canonical(payload))).rstrip(b"=").decode()
        connection.sendall(frame(canonical(dict(payload=payload, signature=signature))))

    with issuer(handler) as (path, _):
        result = LaunchReceiptIdentityVerifier(
            enrollment=enrollment, issuer_transport=transport(path),
            max_receipt_lifetime_seconds=30, exchange_timeout_ms=1000)
        assert result.implementation_commit() == enrollment.implementation_commit
        result.close()
