"""Synthetic loopback TLS only: no NAS enrollment, deployment or live secret."""
import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import os
import socket
import ssl
import sys
import threading
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.x509.oid import NameOID
import pytest

from engineering_hub import current_executor_transport as wire

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="protected Linux files")
NONCE = "b" * 64
TOKEN = b"dedicated-synthetic-executor-only"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@pytest.fixture
def material(tmp_path):
    tmp_path.chmod(0o700)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic observer CA")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    ca = cert.public_bytes(serialization.Encoding.PEM)
    key_bytes = key.private_bytes(serialization.Encoding.PEM,
                                 serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    ca_path, key_path, token_path = (tmp_path / n for n in ("ca.pem", "key.pem", "executor.token"))
    ca_path.write_bytes(ca)
    key_path.write_bytes(key_bytes)
    token_path.write_bytes(TOKEN)
    for path in (ca_path, key_path, token_path):
        path.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ca_path, key_path)
    signer = ed25519.Ed25519PrivateKey.generate()
    payload = {"nonce": NONCE, "evidence": "synthetic transport only"}
    envelope = canonical({"payload": payload, "signature": base64.urlsafe_b64encode(
        signer.sign(canonical(payload))).rstrip(b"=").decode()})
    return dict(context=context, body=envelope, ca_file=ca_path,
                ca_sha256="sha256:" + hashlib.sha256(ca).hexdigest(),
                credential_file=token_path,
                credential_sha256="sha256:" + hashlib.sha256(TOKEN).hexdigest())


def response(body, *, status=200, headers=None):
    if headers is None:
        headers = [b"Content-Type: application/json", b"Content-Length: " + str(len(body)).encode()]
    return b"HTTP/1.1 " + str(status).encode() + b" Test\r\n" + b"\r\n".join(headers) + b"\r\n\r\n" + body


@contextmanager
def server(material, send, *, tls=True):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(.05)
    stopped, seen, errors = threading.Event(), [], []
    def worker():
        while not stopped.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                conn.settimeout(1)
                try:
                    if not tls:
                        stopped.wait(.5)
                        continue
                    with material["context"].wrap_socket(conn, server_side=True) as secure:
                        raw = bytearray()
                        while b"\r\n\r\n" not in raw:
                            part = secure.recv(1024)
                            if not part:
                                break
                            raw.extend(part)
                        header, body = bytes(raw).split(b"\r\n\r\n", 1)
                        length = next(int(line.split(b":", 1)[1]) for line in header.split(b"\r\n")
                                      if line.lower().startswith(b"content-length:"))
                        while len(body) < length:
                            body += secure.recv(length - len(body))
                        seen.append((header, body))
                        send(secure, stopped)
                except (OSError, ssl.SSLError):
                    # Certificate denials and deliberately timed-out clients.
                    pass
                except Exception as error:
                    errors.append(type(error).__name__)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        yield "https://127.0.0.1:" + str(listener.getsockname()[1]), seen
    finally:
        stopped.set()
        listener.close()
        thread.join(2)
        assert not thread.is_alive() and not errors


def transport(material, origin, **replace):
    kwargs = {k: material[k] for k in ("ca_file", "ca_sha256", "credential_file", "credential_sha256")}
    kwargs.update(replace)
    return wire.CurrentExecutorHTTPSTransport(origin, **kwargs)


def test_real_tls_exact_single_request_and_no_environment_discovery(material, monkeypatch):
    for name in ("HTTPS_PROXY", "ALL_PROXY", "SSL_CERT_FILE", "SSLKEYLOGFILE"):
        monkeypatch.setenv(name, "/deliberately/unavailable")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: pytest.fail("DNS discovery"))
    with server(material, lambda conn, _: conn.sendall(response(material["body"]))) as (origin, seen):
        client = transport(material, origin)
        assert client.observe_current_launch(nonce=NONCE, timeout_ms=1000) == material["body"]
        assert len(seen) == 1
        header, body = seen[0]
        assert header.split(b"\r\n")[0] == b"POST /v1/container-pilot/current-observation HTTP/1.1"
        assert b"Authorization: Bearer " + TOKEN in header
        assert body == canonical({"nonce": NONCE})
        assert TOKEN.decode() not in repr(client)
        assert client._context.check_hostname and client._context.verify_mode == ssl.CERT_REQUIRED
        assert client._context.keylog_filename is None


def test_material_captured_once_not_reopened_for_tls_or_request(material):
    with server(material, lambda conn, _: conn.sendall(response(material["body"]))) as (origin, seen):
        client = transport(material, origin)
        material["ca_file"].write_bytes(b"changed after enrollment")
        material["credential_file"].write_bytes(b"changed after enrollment")
        assert client.observe_current_launch(nonce=NONCE, timeout_ms=1000) == material["body"]
        assert b"Authorization: Bearer " + TOKEN in seen[0][0]


def test_maximum_canonical_envelope_accepted(material):
    base = {"payload": {"padding": ""}, "signature": "x"}
    base["payload"]["padding"] = "x" * (8192 - len(canonical(base)))
    body = canonical(base)
    assert len(body) == 8192
    with server(material, lambda conn, _: conn.sendall(response(body))) as (origin, seen):
        assert transport(material, origin).observe_current_launch(nonce=NONCE, timeout_ms=1000) == body
        assert len(seen) == 1


def test_deadline_not_reset_between_headers_and_body(material):
    raw = response(material["body"])
    header, body = raw.split(b"\r\n\r\n", 1)
    def delayed(conn, stopped):
        if stopped.wait(.09): return
        conn.sendall(header + b"\r\n\r\n")
        if stopped.wait(.09): return
        conn.sendall(body)
    with server(material, delayed) as (origin, seen):
        start = time.monotonic()
        with pytest.raises(wire.CurrentExecutorTransportError):
            transport(material, origin).observe_current_launch(nonce=NONCE, timeout_ms=150)
        assert time.monotonic() - start < .4
        assert len(seen) == 1


@pytest.mark.parametrize("origin", ["http://127.0.0.1", "https://localhost", "https://example.com",
    "https://user@127.0.0.1", "https://127.0.0.1/", "https://127.0.0.1?x", "https://127.0.0.1#x",
    "https://127.0.0.1:0", "https://127.0.0.1:65536", "https://127.0.0.1\\x", "https://127.0.0.1\n",
    "https://[fe80::1%eth0]"])
def test_origin_denied_before_material_read(material, monkeypatch, origin):
    monkeypatch.setattr(wire, "read_protected_file", lambda *a, **kw: pytest.fail("read before validation"))
    with pytest.raises(wire.CurrentExecutorTransportError):
        transport(material, origin)


@pytest.mark.parametrize("field", ["ca_sha256", "credential_sha256"])
def test_wrong_material_pin(material, field):
    with pytest.raises(wire.CurrentExecutorTransportError):
        transport(material, "https://127.0.0.1", **{field: "sha256:" + "0" * 64})


def test_protected_token_and_symlink_denied(material):
    path = material["credential_file"]
    path.chmod(0o644)
    with pytest.raises(wire.CurrentExecutorTransportError):
        transport(material, "https://127.0.0.1")
    path.chmod(0o600)
    link = path.parent / "token-link"
    link.symlink_to(path)
    with pytest.raises(wire.CurrentExecutorTransportError):
        transport(material, "https://127.0.0.1", credential_file=link)


@pytest.mark.parametrize("case", ["wrong-ca", "wrong-host"])
def test_real_tls_certificate_denial_before_bearer(material, case):
    if case == "wrong-ca":
        # Another synthetic CA with distinct key, keeping original serving cert.
        import copy
        wrong = copy.copy(material)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "wrong")])
        now = datetime.now(timezone.utc)
        ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
              .public_key(key.public_key()).serial_number(123)
              .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
              .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
              .sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM))
        path = material["ca_file"].parent / "wrong.pem"
        path.write_bytes(ca); path.chmod(0o600)
        wrong.update(ca_file=path, ca_sha256="sha256:" + hashlib.sha256(ca).hexdigest())
    else:
        wrong = material
    with server(material, lambda conn, _: conn.sendall(response(material["body"]))) as (origin, seen):
        client = transport(wrong, origin)
        if case == "wrong-host":
            client._address = "127.0.0.2"
            # Connect to the same socket while validating a different IP SAN.
            original = wire.socket.socket
            class Routed(original):
                def connect(self, address):
                    return super().connect(("127.0.0.1", address[1]))
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(wire.socket, "socket", Routed)
                with pytest.raises(wire.CurrentExecutorTransportError):
                    client.observe_current_launch(nonce=NONCE, timeout_ms=500)
        else:
            with pytest.raises(wire.CurrentExecutorTransportError):
                client.observe_current_launch(nonce=NONCE, timeout_ms=500)
        assert seen == []


@pytest.mark.parametrize("raw", [
    response(b"{}", status=302), response(b"{}", status=401), response(b"{}", status=100),
    response(b"{}", headers=[b"Content-Type: application/json"]),
    response(b"{}", headers=[b"Content-Type: application/json", b"Content-Length: 2", b"content-length: 2"]),
    response(b"{}", headers=[b"Content-Type: application/json", b"Content-Length: 2", b"Transfer-Encoding: chunked"]),
    response(b"{}", headers=[b"Content-Type: application/json", b"Content-Length: 2", b"Content-Encoding: gzip"]),
    response(b"{}", headers=[b"Content-Type: application/json", b"Content-Length: 8193"]),
    response(b"{}", headers=[b"Content-Type: application/json", b"Content-Length: +2"]),
    response(b"{}", headers=[b"Content-Type: application/json", b"Content-Length: 02"]),
    response(b"{}", headers=[b"Content-Type: text/plain", b"Content-Length: 2"]),
    response(b"{}", headers=[b"Content-Type: application/json", b"Content-Length: 2", b" bad: folded"]),
    response(b"{}", headers=[b"Malformed", b"Content-Length: 2"]),
    response(b"{}", headers=[b"X-Large: " + b"x" * 8192]),
    response(b"{}", headers=[b"X-" + str(i).encode() + b": x" for i in range(65)]),
    response(b"{}") + b"extra", response(b'{"payload":{},"payload":{},"signature":"x"}'),
    response(b"x", headers=[b"Content-Type: application/json", b"Content-Length: 2"]),
    response(b'{ "payload":{},"signature":"x"}'), response(b'{"payload":{},"signature":"x","extra":0}'),
    response(b'{"payload":{"x":NaN},"signature":"x"}'), response(b'[]'),
])
def test_reject_wire_without_retry_or_reflection(material, raw):
    with server(material, lambda conn, _: conn.sendall(raw)) as (origin, seen):
        with pytest.raises(wire.CurrentExecutorTransportError) as error:
            transport(material, origin).observe_current_launch(nonce=NONCE, timeout_ms=1000)
        assert str(error.value) == "Current executor transport unavailable"
        assert len(seen) == 1


@pytest.mark.parametrize("stage", ["handshake", "headers", "body", "eof"])
def test_one_deadline_bounds_real_tls_slow_drip(material, stage):
    raw = response(material["body"])
    def drip(conn, stopped):
        if stage == "eof":
            conn.sendall(raw)
            stopped.wait(.5)
            return
        prefix = b"" if stage == "headers" else raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
        conn.sendall(prefix)
        for byte in raw if stage == "headers" else material["body"]:
            if stopped.wait(.025):
                return
            conn.sendall(bytes([byte]))
    with server(material, drip, tls=stage != "handshake") as (origin, seen):
        client = transport(material, origin)
        start = time.monotonic()
        with pytest.raises(wire.CurrentExecutorTransportError):
            client.observe_current_launch(nonce=NONCE, timeout_ms=150)
        assert time.monotonic() - start < .4
        assert len(seen) == (0 if stage == "handshake" else 1)


@pytest.mark.parametrize("nonce,timeout", [("x", 1000), (NONCE, True), (NONCE, 0), (NONCE, 10001)])
def test_invalid_call_no_socket(material, monkeypatch, nonce, timeout):
    client = transport(material, "https://127.0.0.1")
    monkeypatch.setattr(wire.socket, "socket", lambda *a, **kw: pytest.fail("socket allocated"))
    with pytest.raises(wire.CurrentExecutorTransportError):
        client.observe_current_launch(nonce=nonce, timeout_ms=timeout)


def test_fork_fence_no_socket(material, monkeypatch):
    client = transport(material, "https://127.0.0.1")
    monkeypatch.setattr(wire.os, "getpid", lambda: client._pid + 1)
    monkeypatch.setattr(wire.socket, "socket", lambda *a, **kw: pytest.fail("socket allocated"))
    with pytest.raises(wire.CurrentExecutorTransportError):
        client.observe_current_launch(nonce=NONCE, timeout_ms=1000)


@pytest.mark.parametrize("budget", [1, 99, 100])
def test_remaining_observer_budget_accepted_without_renewal(material, monkeypatch, budget):
    client = transport(material, "https://127.0.0.1")
    timeouts, connections = [], []
    class Socket:
        def settimeout(self, value):
            timeouts.append(value)
        def connect(self, address):
            connections.append(address)
            raise TimeoutError()
        def close(self):
            pass
    monkeypatch.setattr(wire.socket, "socket", lambda *a, **kw: Socket())
    with pytest.raises(wire.CurrentExecutorTransportError):
        client.observe_current_launch(nonce=NONCE, timeout_ms=budget)
    assert connections == [("127.0.0.1", 443)]
    assert len(timeouts) == 1 and 0 < timeouts[0] <= budget / 1000


def captured(material, origin, **changes):
    kwargs = dict(ca_pem=material["ca_file"].read_bytes(), ca_sha256=material["ca_sha256"],
                  credential=TOKEN, credential_sha256=material["credential_sha256"])
    kwargs.update(changes)
    return wire.CurrentExecutorHTTPSTransport.from_captured_material(origin, **kwargs)


def test_captured_material_constructor_no_file_or_network_io_and_real_exchange(material, monkeypatch):
    ca = material["ca_file"].read_bytes()
    with server(material, lambda conn, _: conn.sendall(response(material["body"]))) as (origin, seen):
        with monkeypatch.context() as patch:
            patch.setattr(wire, "read_protected_file", lambda *a, **kw: pytest.fail("file read"))
            patch.setattr(wire.socket, "socket", lambda *a, **kw: pytest.fail("socket allocated"))
            patch.setattr(wire.socket, "getaddrinfo", lambda *a, **kw: pytest.fail("DNS lookup"))
            client = wire.CurrentExecutorHTTPSTransport.from_captured_material(origin,
                ca_pem=ca, ca_sha256=material["ca_sha256"], credential=TOKEN,
                credential_sha256=material["credential_sha256"])
        material["ca_file"].unlink()
        material["credential_file"].unlink()
        assert client.observe_current_launch(nonce=NONCE, timeout_ms=1000) == material["body"]
        assert len(seen) == 1 and b"Authorization: Bearer " + TOKEN in seen[0][0]
        assert client._context.check_hostname and client._context.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize("field,value", [
    ("ca_pem", b""), ("ca_pem", b"x" * (1024 * 1024 + 1)),
    ("ca_pem", bytearray(b"x")), ("ca_pem", memoryview(b"x")), ("ca_pem", "not bytes"),
    ("ca_pem", b"not a PEM certificate"), ("ca_pem", b"\xff"),
    ("credential", b""), ("credential", b"x" * 4097),
    ("credential", bytearray(TOKEN)), ("credential", memoryview(TOKEN)), ("credential", "not bytes"),
    ("credential", b"contains space"), ("credential", b"newline\n"), ("credential", b"\x7f"),
    ("ca_sha256", "sha256:" + "0" * 64), ("credential_sha256", "sha256:" + "0" * 64),
    ("ca_sha256", None), ("credential_sha256", "invalid"),
])
def test_captured_material_invalid_types_sizes_pins_and_tls_sanitized(material, monkeypatch, field, value):
    changes = {field: value}
    if field in {"ca_pem", "credential"} and type(value) is bytes:
        changes["ca_sha256" if field == "ca_pem" else "credential_sha256"] = (
            "sha256:" + hashlib.sha256(value).hexdigest())
    monkeypatch.setattr(wire, "read_protected_file", lambda *a, **kw: pytest.fail("file read"))
    with pytest.raises(wire.CurrentExecutorTransportError) as error:
        captured(material, "https://127.0.0.1", **changes)
    assert str(error.value) == "Invalid current executor HTTPS configuration"


@pytest.mark.parametrize("origin", ["http://127.0.0.1", "https://localhost", "https://user@127.0.0.1",
    "https://127.0.0.1/", "https://127.0.0.1?x", "https://127.0.0.1#x", "https://[fe80::1%eth0]",
    "https://127.0.0.1:0", "https://127.0.0.1\\x"])
def test_captured_material_same_origin_denials(material, origin):
    with pytest.raises(wire.CurrentExecutorTransportError):
        captured(material, origin)


def test_captured_material_maximum_credential_bytes(material):
    credential = b"x" * 4096
    client = captured(material, "https://127.0.0.1", credential=credential,
                      credential_sha256="sha256:" + hashlib.sha256(credential).hexdigest())
    assert client._pid == os.getpid()
