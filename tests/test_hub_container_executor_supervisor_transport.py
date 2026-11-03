"""Hub -> supervisor transport for reserve / challenge / dispatch / expiry (docs/supervisor-dispatch-transport.md).

Fixture keys, tokens and certificates are generated here and nowhere else. Loopback only; no load loops;
order is asserted with test-owned barriers, never with wall-clock slack.
"""
import base64
import datetime
import json
import os
from pathlib import Path
import secrets
import socket
import tempfile
import threading
import time
from uuid import uuid4

import pytest

pytest.importorskip("fcntl", reason="Route journal requires actual Linux kernel ownership")

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from hub_container_executor import supervisor_route as route_module
from hub_container_executor.supervisor_route import (
    CHALLENGE_SCOPE, DISPATCH_SCOPE, EXPIRE_SCOPE, INSPECTION_PATH, INSPECTION_SCOPE, RESERVE_SCOPE,
    TRANSPORT_CHALLENGE_PATH, TRANSPORT_DISPATCH_PATH, TRANSPORT_EXPIRE_PATH, TRANSPORT_RESERVE_PATH,
    DispatchCounterGate, ExpiredStatementVerifier, HubUnixIdentity, InspectionResponderConfig,
    InspectionTokenAuthenticator, PinnedKeyInspectionVerifier, RouteTokenAuthenticator, SupervisorInspectionResponder,
    SupervisorRouteDisabled, SupervisorRouteError, SupervisorTransportClient, SupervisorTransportResponder,
    SupervisorTransportServer, TlsServerIdentity, TlsTransportChannel, TransportClientConfig,
    TransportResponderConfig, TransportServerConfig, UnixSocketBackend, UnixTransportChannel, challenge_expectation,
    inspection_key_id, inspection_request, reservation_expectation, spki_sha256, transport_command,
)
from tests.test_hub_container_executor_supervisor_route import (  # noqa: F401  (world is a fixture)
    LINUX, TOKEN, Backend, canonical, command, world,
)
from tests.test_hub_container_executor_supervisor_reservation import (  # noqa: F401  (two is a fixture)
    FixtureSigner, hub_proof, nonce_of, two,
)
from tests.test_hub_container_executor_supervisor_dispatch_proof import (  # noqa: F401  (proof is a fixture)
    proof, route_for,
)
from tests.test_hub_container_executor_supervisor_launch_counter import gate_config
from tests.test_hub_container_executor_supervisor_inspection import INSPECT_TOKEN, own_peer, raw_key

# Test-only transport credentials, one per scope.
TOKENS = {RESERVE_SCOPE: "ehrr1." + "Res3rve" * 6, CHALLENGE_SCOPE: "ehrc1." + "Chall3ng" * 5,
          DISPATCH_SCOPE: "ehrd1." + "D1spatch" * 5, EXPIRE_SCOPE: "ehre1." + "Exp1re" * 7,
          INSPECTION_SCOPE: INSPECT_TOKEN}
ROUTES = {TRANSPORT_RESERVE_PATH: RESERVE_SCOPE, TRANSPORT_CHALLENGE_PATH: CHALLENGE_SCOPE,
          TRANSPORT_DISPATCH_PATH: DISPATCH_SCOPE, TRANSPORT_EXPIRE_PATH: EXPIRE_SCOPE,
          INSPECTION_PATH: INSPECTION_SCOPE}
DISPATCH_PATH_SCOPES = (RESERVE_SCOPE, CHALLENGE_SCOPE, DISPATCH_SCOPE)
RESOLVER_SCOPES = (EXPIRE_SCOPE, INSPECTION_SCOPE)
GUARD_S = 10.0  # Hang guard for every event wait; never a latency assertion.


# ---------------------------------------------------------------- fixtures

class MarkerExecutor(Backend):
    """Fake executor: DispatchCounterGate.run fences a marker-file effect."""
    carries_dispatch = True

    def __init__(self, pin, gate, marker):
        super().__init__(pin)
        self.gate, self.marker = gate, marker

    def forward(self, *, dispatch, **kwargs):
        def effect():
            with open(self.marker, "a", encoding="ascii") as handle:
                handle.write(kwargs["body"].hex()[:16] + "\n")
            return Backend.forward(self, **kwargs)
        return self.gate.run(dispatch, method=kwargs["method"], path=kwargs["path"], body=kwargs["body"],
                             effect=effect)


def marker_lines(env):
    path = Path(env["marker"])
    return path.read_text(encoding="ascii").splitlines() if path.exists() else []


def make_identity(directory, name):
    """Test-only self-signed EC P-256 TLS identity (never a signing key)."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM)
    cert_path, key_path = Path(directory) / f"{name}.crt", Path(directory) / f"{name}.key"
    cert_path.write_bytes(pem)
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    os.chmod(key_path, 0o600)
    return {"cert": str(cert_path), "key": str(key_path), "pem": pem, "spki": spki_sha256(pem)}


def bundle(directory, name, *identities):
    path = Path(directory) / f"{name}.pem"
    path.write_bytes(b"".join(identity["pem"] for identity in identities))
    return str(path)


class Listener:
    """Test-side accept loop: one thread per connection -> serve_connection."""

    def __init__(self, sock, server):
        self.sock, self.server, self.outcomes, self.threads = sock, server, [], []
        self.changed = threading.Condition()
        self.acceptor = threading.Thread(target=self._accept, daemon=True)
        self.acceptor.start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            thread = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            thread.start()
            self.threads.append(thread)

    def _serve(self, conn):
        outcome = self.server.serve_connection(conn)
        with self.changed:
            self.outcomes.append(outcome)
            self.changed.notify_all()

    def wait_outcomes(self, count):
        with self.changed:
            assert self.changed.wait_for(lambda: len(self.outcomes) >= count, GUARD_S), self.outcomes
            return list(self.outcomes)

    def close(self):
        self.sock.close()
        for thread in self.threads:
            thread.join(GUARD_S)


class StallingServer(SupervisorTransportServer):
    """Reply seam owned by the test: drop, stall or trickle the next reply."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.mode = None
        self.entered, self.release, self.left = threading.Event(), threading.Event(), threading.Event()

    def _reply(self, peer, status, raw):
        mode, self.mode = self.mode, None
        if mode is None:
            return super()._reply(peer, status, raw)
        try:
            self.entered.set()
            if mode == "drop":
                return
            head = b"HTTP/1.1 %d X\r\nContent-Length: %d\r\n\r\n" % (status, len(raw))
            if mode == "stall":
                peer.sendall(head + raw[:5])
                self.release.wait(GUARD_S + 5)
                return
            peer.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")  # Trickle sub-timeout chunks until released.
            while not self.release.wait(0.05):
                peer.sendall(b"a")
        except OSError:
            self.release.wait(GUARD_S + 5)  # The client gave up; the lifetime stays the test's.
        finally:
            self.left.set()


@pytest.fixture
def env(proof, tmp_path):
    marker = tmp_path / "marker.log"
    executor_dir = tmp_path / "executor"
    executor_dir.mkdir(mode=0o700)
    gate = DispatchCounterGate(config=gate_config(executor_dir / "gate.sqlite"))
    executor = MarkerExecutor(proof["pin"], gate, str(marker))
    route = route_for(proof, backend=executor)
    inspection_key = Ed25519PrivateKey.generate()  # Test-only supervisor inspection key.
    inspection = SupervisorInspectionResponder(config=InspectionResponderConfig(enabled=True), route=route,
                                               authenticator=InspectionTokenAuthenticator(INSPECT_TOKEN),
                                               signing_key=inspection_key)
    tls_dir = tmp_path / "tls"
    tls_dir.mkdir(mode=0o700)
    state = {"proof": proof, "route": route, "gate": gate, "executor": executor, "marker": str(marker),
             "inspection_key": inspection_key, "inspection": inspection, "tls_dir": str(tls_dir),
             "listeners": [], "applied": {}}

    def responder(**config):
        return SupervisorTransportResponder(
            config=TransportResponderConfig(enabled=True, **config), route=route,
            authenticators=[RouteTokenAuthenticator(s, TOKENS[s]) for s in (RESERVE_SCOPE, CHALLENGE_SCOPE,
                                                                             DISPATCH_SCOPE, EXPIRE_SCOPE)],
            inspection=inspection)

    def unix_server(identity=None, responder_config=None, server_class=StallingServer, request_timeout_ms=2000):
        directory = tempfile.mkdtemp(dir="/tmp")
        path = os.path.join(directory, "transport.sock")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(path)
        sock.listen(16)
        server = server_class(config=TransportServerConfig(enabled=True, request_timeout_ms=request_timeout_ms),
                              responder=responder(**(responder_config or {})),
                              identity=identity or HubUnixIdentity(os.getuid(), os.getgid()))
        listener = Listener(sock, server)
        state["listeners"].append((listener, path, directory))
        return listener, path

    def tls_server(server_identity, client_anchor, client_spki):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        server = StallingServer(config=TransportServerConfig(enabled=True), responder=responder(),
                                identity=TlsServerIdentity(server_identity["cert"], server_identity["key"],
                                                           client_anchor, client_spki))
        listener = Listener(sock, server)
        state["listeners"].append((listener, None, None))
        return listener, sock.getsockname()[1]

    def client(channel, scopes, timeout_ms=2000):
        return SupervisorTransportClient(config=TransportClientConfig(enabled=True, timeout_ms=timeout_ms),
                                         channel=channel, credentials={s: TOKENS[s] for s in scopes})
    state.update(responder=responder, unix_server=unix_server, tls_server=tls_server, client=client)
    try:
        yield state
    finally:
        for listener, path, directory in state["listeners"]:
            listener.server.release.set()
            listener.close()
            if path:
                os.unlink(path)
                os.rmdir(directory)
        try:
            route.close()
        finally:
            gate.close()


def unix_clients(env, path, peer=None, timeout_ms=2000):
    channel = UnixTransportChannel(path, peer or own_peer())
    return env["client"](channel, DISPATCH_PATH_SCOPES, timeout_ms), env["client"](channel, RESOLVER_SCOPES, timeout_ms)


def tls_identities(env):
    directory = env["tls_dir"]
    return {name: make_identity(directory, name) for name in ("supervisor", "hub", "rogue")}


def tls_clients(env, port, ids, *, client_id="hub", anchor=None, server_spki=None):
    channel = TlsTransportChannel("127.0.0.1", port, ids[client_id]["cert"], ids[client_id]["key"],
                                  anchor or ids["supervisor"]["cert"], server_spki or ids["supervisor"]["spki"])
    return env["client"](channel, DISPATCH_PATH_SCOPES), env["client"](channel, RESOLVER_SCOPES)


# ---------------------------------------------------------------- Hub-side flow (verifies everything itself)

def hub_reserve_and_prove(env, hub, cmd):
    proof = env["proof"]
    reserved = hub.reserve(cmd)
    assert not reserved.held, reserved
    receipt = reserved.raw
    proof["verifier"].verify(receipt, expected=reservation_expectation(cmd, proof["pin"]))  # Own pinned key.
    issued = hub.challenge(cmd.command_id)
    assert not issued.held, issued
    proof["challenge_verifier"].verify(issued.raw, expected=challenge_expectation(receipt, proof["pin"],
                                                                                    proof["epoch"]))
    nonce = nonce_of(issued.raw)
    return receipt, nonce, hub_proof(proof, receipt, cmd, nonce)


def hub_inspect_and_apply(env, resolver, cmd, receipt):
    """Settles the attempt ONLY from a verified signed inspection answer."""
    key = raw_key(env["inspection_key"])
    counter = json.loads(receipt)["payload"]["counter"]
    request = inspection_request(cmd, env["proof"]["pin"], counter, key_id=inspection_key_id(key),
                                 request_nonce=secrets.token_hex(16))
    answer = resolver.inspect(request)
    assert not answer.held, answer
    payload = PinnedKeyInspectionVerifier(key).verify(answer.raw, request=request)
    previous = env["applied"].setdefault(cmd.command_id, payload["status"])
    assert previous == payload["status"]
    return payload["status"]


def full_flow(env, hub, resolver):
    cmd = command(env["proof"]["epoch"])
    receipt, nonce, signed = hub_reserve_and_prove(env, hub, cmd)
    sent = hub.dispatch(receipt, cmd, signed)
    assert sent.held and sent.hint["state"] == "completed"  # Unsigned hint: recorded, never applied.
    assert cmd.command_id not in env["applied"]
    assert hub_inspect_and_apply(env, resolver, cmd, receipt) == "launched"
    assert hub_inspect_and_apply(env, resolver, cmd, receipt) == "launched"  # Re-applying is a no-op.
    return cmd, nonce


# ---------------------------------------------------------------- 0. disabled / default-refusing

def test_transport_is_disabled_by_default_and_refuses_bad_credentials(world):
    for build in (lambda: SupervisorTransportResponder(config=TransportResponderConfig(), route=None),
                  lambda: SupervisorTransportServer(config=TransportServerConfig(), responder=None, identity=None),
                  lambda: SupervisorTransportClient(config=TransportClientConfig(), channel=None, credentials={})):
        with pytest.raises(SupervisorRouteDisabled):
            build()
    with pytest.raises(SupervisorRouteError):
        RouteTokenAuthenticator(INSPECTION_SCOPE, INSPECT_TOKEN)  # Inspect keeps its own authenticator.
    for scope, token in TOKENS.items():
        for other in (RESERVE_SCOPE, CHALLENGE_SCOPE, DISPATCH_SCOPE, EXPIRE_SCOPE):
            if other != scope:
                with pytest.raises(SupervisorRouteError):
                    RouteTokenAuthenticator(other, token)  # Wrong prefix: cannot even be enrolled.
        with pytest.raises(SupervisorRouteError):
            UnixSocketBackend("/tmp/x.sock", bearer_token=token)  # Never the legacy launch bearer.
    with pytest.raises(SupervisorRouteError):
        RouteTokenAuthenticator(RESERVE_SCOPE, TOKEN)  # The launch bearer cannot be enrolled either.
    assert "Res3rve" not in repr(RouteTokenAuthenticator(RESERVE_SCOPE, TOKENS[RESERVE_SCOPE]))


# ---------------------------------------------------------------- 1. happy path over a Unix socket

@LINUX
def test_happy_path_over_unix_socket(env):
    listener, path = env["unix_server"]()
    hub, resolver = unix_clients(env, path)
    with pytest.raises(SupervisorRouteError):
        hub.expired_statement("A" * 43)  # No expire credential on the dispatch path: refused before connecting.
    with pytest.raises(SupervisorRouteError):
        resolver.reserve(command(env["proof"]["epoch"]))  # No reserve credential on the resolver.
    cmd, _ = full_flow(env, hub, resolver)
    assert len(marker_lines(env)) == 1  # The fenced effect ran exactly once.
    assert env["gate"].inspect()["accepted"] == 1


# ---------------------------------------------------------------- 2. same path over mTLS

@LINUX
def test_happy_path_over_mutual_tls_with_pinned_spki(env):
    ids = tls_identities(env)
    listener, port = env["tls_server"](ids["supervisor"], ids["hub"]["cert"], ids["hub"]["spki"])
    hub, resolver = tls_clients(env, port, ids)
    full_flow(env, hub, resolver)
    assert len(marker_lines(env)) == 1
    assert set(listener.wait_outcomes(5)) == {"answered"}


# ---------------------------------------------------------------- raw requests (tests only)

def raw_exchange(path, request_bytes):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(GUARD_S)
        sock.connect(path)
        sock.sendall(request_bytes)
        return route_module._read_response(sock, lambda: GUARD_S)
    finally:
        sock.close()


def http_request(path, authorization, body, content_length=None):
    head = [f"POST {path} HTTP/1.1", "Host: t", "Connection: close", "Content-Type: application/json",
            f"Content-Length: {len(body) if content_length is None else content_length}"]
    if authorization is not None:
        head.append("Authorization: " + authorization)
    return ("\r\n".join(head) + "\r\n\r\n").encode("ascii") + body


def route_bodies(env):
    """One well-formed body per route, so only authentication decides."""
    proof = env["proof"]
    cmd = command(proof["epoch"])
    receipt = env["route"].reserve(cmd).receipt
    request = inspection_request(cmd, proof["pin"], 1, key_id=inspection_key_id(raw_key(env["inspection_key"])),
                                 request_nonce=secrets.token_hex(16))
    nonce = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    return {TRANSPORT_RESERVE_PATH: canonical({"command": transport_command(command(proof["epoch"]))}),
            TRANSPORT_CHALLENGE_PATH: canonical({"command_id": cmd.command_id}),
            TRANSPORT_DISPATCH_PATH: canonical({"command": transport_command(cmd),
                                                "proof": base64.urlsafe_b64encode(b"{}").rstrip(b"=").decode(),
                                                "receipt": base64.urlsafe_b64encode(receipt).rstrip(b"=").decode()}),
            TRANSPORT_EXPIRE_PATH: canonical({"nonce": nonce}),
            INSPECTION_PATH: canonical(request)}


# ---------------------------------------------------------------- 3. scope matrix

@LINUX
def test_scope_matrix_each_token_only_on_its_own_route(env):
    listener, path = env["unix_server"]()
    bodies = route_bodies(env)
    for route_path, own_scope in ROUTES.items():
        for scope, token in [*TOKENS.items(), ("legacy-launch", TOKEN), ("none", None)]:
            status, raw = raw_exchange(path, http_request(route_path, None if token is None else "Bearer " + token,
                                                          bodies[route_path]))
            if scope == own_scope:
                assert status != 401, (route_path, scope, status, raw)
            else:
                assert (status, json.loads(raw)) == (401, {"error": {"code": "unauthorized"}}), (route_path, scope)
    assert len(marker_lines(env)) == 0


# ---------------------------------------------------------------- 4. identity

@LINUX
def test_wrong_supervisor_peer_pin_sends_zero_bytes(env):
    listener, path = env["unix_server"]()
    hub, _ = unix_clients(env, path, peer=own_peer(ticks_offset=1))
    cmd = command(env["proof"]["epoch"])
    reply = hub.reserve(cmd)
    assert (reply.held, reply.error) == (True, "peer_mismatch")
    assert listener.wait_outcomes(1) == ["framing"]  # EOF before a single request byte.
    assert env["route"].inspect_reservation(cmd.command_id) is None


@LINUX
def test_wrong_hub_uid_is_refused_before_reading(env):
    listener, path = env["unix_server"](identity=HubUnixIdentity(os.getuid() + 1, os.getgid()))
    hub, _ = unix_clients(env, path)
    cmd = command(env["proof"]["epoch"])
    reply = hub.reserve(cmd)
    assert reply.held and reply.error == "transport_lost"
    assert listener.wait_outcomes(1) == ["peer_refused"]
    assert env["route"].inspect_reservation(cmd.command_id) is None  # Nothing was processed.


@LINUX
def test_wrong_server_spki_refused_by_hub_before_application_bytes(env):
    ids = tls_identities(env)
    listener, port = env["tls_server"](ids["supervisor"], ids["hub"]["cert"], ids["hub"]["spki"])
    cmd = command(env["proof"]["epoch"])
    hub, _ = tls_clients(env, port, ids, server_spki=ids["rogue"]["spki"])  # Pin names another key.
    assert hub.reserve(cmd).error == "peer_mismatch"
    assert listener.wait_outcomes(1) == ["framing"]  # Handshake only; no request byte ever arrived.
    assert env["route"].inspect_reservation(cmd.command_id) is None


@LINUX
def test_over_broad_anchor_still_cannot_admit_another_server_key(env):
    ids = tls_identities(env)
    # The supervisor presents the ROGUE certificate; the Hub's anchor file wrongly also trusts it.
    listener, port = env["tls_server"](ids["rogue"], ids["hub"]["cert"], ids["hub"]["spki"])
    cmd = command(env["proof"]["epoch"])
    anchor = bundle(env["tls_dir"], "hub-anchor", ids["supervisor"], ids["rogue"])
    hub, _ = tls_clients(env, port, ids, anchor=anchor)
    assert hub.reserve(cmd).error == "peer_mismatch"  # Only the SPKI pin refuses it.
    assert listener.wait_outcomes(1) == ["framing"]
    assert env["route"].inspect_reservation(cmd.command_id) is None


@LINUX
@pytest.mark.parametrize("case", ["client_not_anchored", "client_anchored_wrong_spki"])
def test_wrong_client_identity_refused_by_supervisor_before_reading(env, case):
    ids = tls_identities(env)
    anchor = ids["hub"]["cert"] if case == "client_not_anchored" else bundle(env["tls_dir"], "sup-anchor",
                                                                             ids["hub"], ids["rogue"])
    listener, port = env["tls_server"](ids["supervisor"], anchor, ids["hub"]["spki"])
    cmd = command(env["proof"]["epoch"])
    hub, _ = tls_clients(env, port, ids, client_id="rogue")
    reply = hub.reserve(cmd)
    assert reply.held and reply.error in ("transport_lost", "peer_mismatch")
    assert listener.wait_outcomes(1) == ["peer_refused"]  # Refused before any application byte was read.
    assert env["route"].inspect_reservation(cmd.command_id) is None


# ---------------------------------------------------------------- 5. dispatch reply lost

@LINUX
def test_lost_dispatch_reply_is_held_and_resolved_by_inspection_once(env):
    listener, path = env["unix_server"]()
    hub, resolver = unix_clients(env, path)
    cmd = command(env["proof"]["epoch"])
    receipt, _, signed = hub_reserve_and_prove(env, hub, cmd)
    listener.server.mode = "drop"
    lost = hub.dispatch(receipt, cmd, signed)
    assert lost.held and lost.hint is None and lost.error == "transport_lost"
    assert len(marker_lines(env)) == 1  # It did run on the supervisor side...
    assert cmd.command_id not in env["applied"]  # ...but the Hub moved nothing on a lost reply.
    assert hub_inspect_and_apply(env, resolver, cmd, receipt) == "launched"
    again = hub.dispatch(receipt, cmd, signed)  # Never re-sent under the same proof or counter.
    assert again.held and again.hint["state"] == "held" and len(marker_lines(env)) == 1


# ---------------------------------------------------------------- 6. crash before dispatch

@LINUX
def test_crash_before_dispatch_is_retired_by_expired_statement(env):
    listener, path = env["unix_server"]()
    hub, resolver = unix_clients(env, path)
    cmd = command(env["proof"]["epoch"])
    receipt, nonce, signed = hub_reserve_and_prove(env, hub, cmd)  # Proof made; the Hub "crashes" here.
    statement = resolver.expired_statement(nonce)
    assert not statement.held
    signer = env["proof"]["statements"]
    payload = ExpiredStatementVerifier(public_key=signer.public_key, key_id=signer.key_id).verify(
        statement.raw, nonce=nonce)
    assert (payload["status"], payload["reservation_status"]) == ("expired_unconsumed", "retired")
    env["applied"][cmd.command_id] = "expired_unsent"  # The barrier is freed from the signed statement only.
    assert resolver.expired_statement(nonce).raw == statement.raw  # Idempotent.
    late = hub.dispatch(receipt, cmd, signed)
    assert late.held and late.hint["error"] == "reservation_abandoned"
    assert marker_lines(env) == []


# ---------------------------------------------------------------- 7. replay

@LINUX
def test_replayed_proof_over_the_transport_runs_the_effect_once(env):
    listener, path = env["unix_server"]()
    hub, resolver = unix_clients(env, path)
    cmd = command(env["proof"]["epoch"])
    receipt, nonce, signed = hub_reserve_and_prove(env, hub, cmd)
    first, second = hub.dispatch(receipt, cmd, signed), hub.dispatch(receipt, cmd, signed)
    assert first.hint["state"] == "completed" and second.held
    assert (second.hint["state"], second.hint["error"]) == ("held", "already_dispatched")
    assert env["route"].inspect_challenge(nonce)[0] == "consumed"
    assert len(marker_lines(env)) == 1


# ---------------------------------------------------------------- 8. bounds

@LINUX
@pytest.mark.parametrize("route_path", [TRANSPORT_DISPATCH_PATH, TRANSPORT_RESERVE_PATH, TRANSPORT_EXPIRE_PATH])
def test_oversize_body_refused_before_it_is_read(env, route_path):
    listener, path = env["unix_server"]()
    cap = SupervisorTransportResponder.body_cap(route_path)
    # Only the headers are sent: the cap is enforced from Content-Length, before reading any body byte.
    status, raw = raw_exchange(path, http_request(route_path, "Bearer " + TOKENS[ROUTES[route_path]], b"",
                                                  content_length=cap + 1))
    assert (status, json.loads(raw)) == (413, {"error": {"code": "too_large"}})
    assert listener.wait_outcomes(1) == ["too_large"]


@LINUX
@pytest.mark.parametrize("mode", ["stall", "trickle"])
def test_stalled_or_trickled_reply_is_held_by_the_client_deadline(env, mode):
    listener, path = env["unix_server"]()
    hub, _ = unix_clients(env, path, timeout_ms=600)
    listener.server.mode = mode
    reply = hub.reserve(command(env["proof"]["epoch"]))
    assert reply.held and reply.error == "transport_lost"
    server = listener.server
    # The supervisor's reply could not end on its own (test-owned barrier): only the client deadline ended it.
    assert server.entered.is_set() and not server.left.is_set() and not server.release.is_set()
    server.release.set()
    assert server.left.wait(GUARD_S)


@LINUX
def test_stalled_request_is_bounded_by_the_server_deadline(env):
    listener, path = env["unix_server"](request_timeout_ms=600)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
        sock.sendall(b"POST " + TRANSPORT_RESERVE_PATH.encode() + b" HTTP/1.1\r\nContent-Le")  # Then nothing.
        assert listener.wait_outcomes(1) == ["timeout"]  # The client never finished: only the deadline ended it.
    finally:
        sock.close()


@LINUX
def test_full_pools_answer_busy_at_once_and_groups_do_not_starve_each_other(env, monkeypatch):
    listener, path = env["unix_server"](responder_config={"max_authenticating": 1, "max_concurrent": 1})
    hub, resolver = unix_clients(env, path)
    responder = listener.server._responder
    entered, release = threading.Event(), threading.Event()
    original = env["route"].reserve

    def held_reserve(command_):
        entered.set()
        assert release.wait(GUARD_S)
        return original(command_)
    monkeypatch.setattr(env["route"], "reserve", held_reserve)
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.setdefault("r", hub.reserve(command(env["proof"]["epoch"]))))
    worker.start()
    try:
        assert entered.wait(GUARD_S)  # Post-auth pool of the dispatch path is full.
        busy = hub.reserve(command(env["proof"]["epoch"]))
        assert (busy.held, busy.status) == (True, 503) and not release.is_set()
        nonce = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        other_group = resolver.expired_statement(nonce)  # Resolver group still served (409: no such nonce).
        assert other_group.status == 409 and not release.is_set()
        auth_entered, auth_release = threading.Event(), threading.Event()
        authenticator = responder._authenticators[EXPIRE_SCOPE]

        def held_authenticate(authorization):
            auth_entered.set()
            assert auth_release.wait(GUARD_S)
            return EXPIRE_SCOPE
        monkeypatch.setattr(authenticator, "authenticate", held_authenticate)
        side = threading.Thread(target=lambda: resolver.expired_statement(nonce))
        side.start()
        try:
            assert auth_entered.wait(GUARD_S)  # Pre-auth pool of the resolver is full.
            assert resolver.expired_statement(nonce).status == 503 and not auth_release.is_set()
        finally:
            auth_release.set()
            side.join(GUARD_S)
    finally:
        release.set()
        worker.join(GUARD_S)
    assert not outcome["r"].held


@LINUX
def test_signed_reply_is_not_returned_after_the_answer_budget(env, monkeypatch):
    listener, path = env["unix_server"](responder_config={"answer_budget_ms": 20})
    hub, _ = unix_clients(env, path)
    original = env["route"].reserve

    def slow_reserve(command_):
        started = time.monotonic()
        while time.monotonic() - started <= 0.05:  # Longer than the budget, in the safe direction only.
            threading.Event().wait(0.01)
        return original(command_)
    monkeypatch.setattr(env["route"], "reserve", slow_reserve)
    cmd = command(env["proof"]["epoch"])
    reply = hub.reserve(cmd)
    assert (reply.held, reply.status) == (True, 503)  # Unsigned; the signed receipt was dropped.
    assert env["route"].inspect_reservation(cmd.command_id)[0] == "reserved"  # It did take effect.


# ---------------------------------------------------------------- 9. unknown fields / non-canonical

@LINUX
def test_unknown_missing_or_non_canonical_requests_refused(env):
    listener, path = env["unix_server"]()
    cmd = command(env["proof"]["epoch"])
    good = {"command": transport_command(cmd)}
    body = transport_command(cmd)
    bad = [
        canonical({**good, "extra": 1}), canonical({}), json.dumps(good).encode(),  # Extra / missing / spaces.
        canonical({"command": {**body, "extra": 1}}), canonical({"command": {k: v for k, v in body.items()
                                                                             if k != "queue_fence"}}),
        canonical({"command": {**body, "body": body["body"] + "="}}),  # Padded base64.
        canonical({"command": {**body, "body": body["body"].replace("-", "+") + "+"}}),
        canonical({"command": {**body, "queue_fence": str(cmd.queue_fence)}}),
        b"not json",
    ]
    for value in bad:
        status, raw = raw_exchange(path, http_request(TRANSPORT_RESERVE_PATH, "Bearer " + TOKENS[RESERVE_SCOPE],
                                                      value))
        assert (status, json.loads(raw)) == (400, {"error": {"code": "invalid_payload"}}), value[:80]
    for route_path, value in ((TRANSPORT_CHALLENGE_PATH, canonical({"command_id": "not-a-uuid"})),
                              (TRANSPORT_EXPIRE_PATH, canonical({"nonce": "short"})),
                              (TRANSPORT_DISPATCH_PATH, canonical({"command": body, "proof": "a", "receipt": "a"}))):
        status, _ = raw_exchange(path, http_request(route_path, "Bearer " + TOKENS[ROUTES[route_path]], value))
        assert status == 400, route_path
    assert env["route"].inspect_reservation(cmd.command_id) is None and marker_lines(env) == []
